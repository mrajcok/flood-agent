"""SQL Cell Runs: everything a SQL Cell's Run does, without the LLM.

A Run takes the Cell's Draft and runs one of three things:
- a SHOW or DESCRIBE TABLE command, whose rows are kept with the Run
- a bare count, which gives a Count Display
- a query, which makes a Result Table, or for a re-run puts new rows under the latest Run's own table through
  table replacement (ADR 0006)

It records the Run in the NotebookStore: its status, its saved result, and the message AI Cells below are sent.
That message is frozen at this Run, and from a table it carries only Query Metadata, never rows (ADR 0001).
"""

import asyncio
import json
import logging
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import AsyncIterator, Callable

from flood_agent import queries, table_names
from flood_agent.catalog import Catalog, CellRun, Table, TableExpired
from flood_agent.events import CountEvent, DisplayEvent, Event, TurnEnd, TurnError
from flood_agent.notebooks import COUNT_KIND, DEFAULT_MAX_ROWS, NotebookStore
from flood_agent.show_commands import SHOW_IN_TEXTBOX, ShowFailed, is_show_statement, run_show, statement_of
from flood_agent.table_replacement import RefreshInProgress, ReplacementFailed, Rerun, TableReplacement

log = logging.getLogger("uvicorn.error")  # the app's terminal log

SHOW_ROWS_TO_AI = 100  # how many rows of a SHOW Cell's result an AI Cell below is sent
CRASHED = "The query could not be run."


# What the blocking part of a Run ended with. Each knows how it is saved with the Run (a stored format the page
# and saved Notebooks read: keep its keys) and how it reads to the AI Cells below.

@dataclass
class _ShowRows:
    columns: list[str]
    rows: list[list]
    command: str
    status = "ok"

    def saved(self) -> dict:
        return {"status": "ok", "columns": self.columns, "rows": self.rows, "row_count": len(self.rows),
                "command": self.command}

    def detail(self) -> str:
        """The columns and up to SHOW_ROWS_TO_AI rows, with a note when rows were left out."""
        lines = [" | ".join(self.columns)]
        lines += [" | ".join("" if v is None else str(v) for v in r) for r in self.rows[:SHOW_ROWS_TO_AI]]
        if len(self.rows) > SHOW_ROWS_TO_AI:
            lines.append(f"(first {SHOW_ROWS_TO_AI} of {len(self.rows)} rows; the other rows were left out)")
        return "\n".join(lines)


@dataclass
class _Count:
    value: int
    status = "ok"

    def saved(self) -> dict:
        return {"status": "ok", "count": self.value}

    def detail(self) -> str:
        return f"Count: {self.value}"


@dataclass
class _TableMade:
    name: str
    columns: list[dict]
    row_count: int
    truncated_to: int | None
    status = "ok"

    def saved(self) -> dict:  # the columns are the table's, not the Run's
        return {"status": "ok", "table": self.name, "row_count": self.row_count, "truncated_to": self.truncated_to}

    def detail(self) -> str:
        meta: dict = {"result_table": self.name, "columns": self.columns, "row_count": self.row_count}
        if self.truncated_to:
            meta["truncated_to"] = self.truncated_to
        return f"Query Metadata: {json.dumps(meta)}"


@dataclass
class _Failed:
    error: str  # value-free: what the AI Cells below are sent
    raw_error: str  # for the user, and logged
    status = "failed"

    def saved(self) -> dict:
        return {"status": "failed", "error": self.error, "raw_error": self.raw_error}

    def detail(self) -> str:
        return f"Failed: {self.error}"


@dataclass
class _Stopped:
    status = "stopped"

    def saved(self) -> dict:
        return {"status": "stopped"}

    def detail(self) -> str:
        return "(stopped by the user)"  # as an AI Cell's stopped Run reads below


_Outcome = _ShowRows | _Count | _TableMade | _Failed | _Stopped


class SqlCellRuns:
    """Runs SQL Cells and stops them. Built once by the Agent; the Agent keeps the rule that one Cell runs per
    Notebook at a time."""

    def __init__(self, notebooks: NotebookStore, tables: Catalog, replacement: TableReplacement,
                 telemetry_db: Path, results_db: Path, timeout_s: float, clock: Callable[[], int]):
        self.notebooks, self.tables, self.replacement = notebooks, tables, replacement
        self.telemetry_db, self.results_db, self.timeout_s, self.clock = telemetry_db, results_db, timeout_s, clock
        self._stops: dict[str, threading.Event] = {}  # SQL Cell id -> its running query's stop flag

    async def run(self, notebook_id: str, cell: dict) -> AsyncIterator[Event]:
        """Run a SQL Cell with what its Draft holds and stream its events: a table or Count Display, an error,
        then the end. Its Run replaces the Cell's earlier one. If the stream is abandoned (the client left), the
        query is abandoned and the Run recorded as stopped."""
        cell_id, draft = cell["id"], cell["draft"]
        sql, max_rows = draft.get("sql", ""), draft.get("max_rows") or DEFAULT_MAX_ROWS
        command, table = draft.get("command") or "", draft.get("table") or ""
        statement = statement_of(command, table) if command else sql
        last = cell["run"]  # the table a re-run replaces: the latest Run's own Result Table, if still live; never a Saved Table
        prior = next((t for t in (self.tables.tables_of_run(cell_id, last["id"]) if last else []) if t.kind == "result"), None)
        run_id = self.notebooks.start_run(cell_id, statement, {"max_rows": max_rows, "command": command}, "")
        stop = self._stops[cell_id] = threading.Event()
        started = time.monotonic()
        try:
            try:
                outcome = await asyncio.to_thread(
                    self._execute, sql, max_rows, stop, prior, CellRun(cell_id, run_id), command, table)
            except asyncio.CancelledError:  # the client left: abandon the query
                stop.set()
                self.notebooks.finish_run(run_id, "stopped")
                raise
            except Exception:
                log.exception("SQL Cell %s crashed", cell_id)
                outcome = _Failed(CRASHED, CRASHED)
        finally:
            self._stops.pop(cell_id, None)
        elapsed_s = round(time.monotonic() - started, 3)

        label = f"[SHOW Cell, run by the user] {statement}" if command else f"[SQL Cell, run by the user] {sql}"
        displays: list[tuple] = []
        event: Event | None = None
        if isinstance(outcome, _Count):
            computed, title = self.clock(), queries.shorten_sql(sql)
            displays.append((COUNT_KIND, None, {"title": title, "value": outcome.value, "sql": sql, "computed_at_ms": computed}))
            event = CountEvent(title, outcome.value, sql, computed)
        elif isinstance(outcome, _TableMade):
            names = [c["name"] for c in outcome.columns]
            displays.append(("table", outcome.name, {"columns": names}))
            event = DisplayEvent("table", outcome.name, names)
        # The message is stored once, so what AI Cells below are sent is frozen at this Run.
        self.notebooks.add_step(run_id, [({"role": "user", "content": f"{label}\n{outcome.detail()}"}, displays)])
        self.notebooks.save_result(run_id, outcome.saved() | {"elapsed_s": elapsed_s})
        self.notebooks.finish_run(run_id, outcome.status)
        if event is not None:
            yield event
        if isinstance(outcome, _Failed):
            yield TurnError(outcome.raw_error, "sql cell failed")
        yield TurnEnd()

    def stop(self, cell_id: str) -> bool:
        """Stop a running SQL Cell now: its query's progress handler sees the flag and aborts it, and the Run is
        recorded as stopped. False if the Cell is not running here."""
        flag = self._stops.get(cell_id)
        if flag is None:
            return False
        flag.set()
        return True

    def _execute(self, sql: str, max_rows: int, stop: threading.Event, prior: Table | None, owner: CellRun,
                 command: str, table: str) -> _Outcome:
        """The blocking part of a Run (it runs in a thread)."""
        if command:  # SHOW / DESCRIBE: no table, no row cap; the rows are kept with the Run
            try:
                names, rows = run_show(command, table, self.telemetry_db, self.results_db, self.tables.listing())
            except ShowFailed as e:
                log.warning("SQL Cell %s failed: %s", owner.cell_id, e)
                return _Failed(str(e), str(e))
            return _ShowRows(names, rows, command)
        if is_show_statement(sql):
            return _Failed(SHOW_IN_TEXTBOX, SHOW_IN_TEXTBOX)
        if queries.ends_with_limit(sql):
            return _Failed(queries.LIMIT_IN_SQL_CELL, queries.LIMIT_IN_SQL_CELL)

        def failure(error: str, raw_error: str | None = None, stopped: bool = False) -> _Outcome:
            if stopped:
                return _Stopped()
            raw = raw_error or error
            log.warning("SQL Cell %s failed: %s", owner.cell_id, raw)
            return _Failed(error, raw)

        readable = self.tables.readable()
        if queries.looks_like_count(sql):
            counted = queries.run_count_query(self.telemetry_db, self.results_db, sql, self.timeout_s, readable, stop)
            if counted["success"]:
                return _Count(counted["count"])
            if counted.get("stopped") or counted["error"] != queries.COUNT_GUIDANCE:  # only the wrong shape falls back to a table
                return failure(counted["error"], counted.get("raw_error"), bool(counted.get("stopped")))
        title = queries.shorten_sql(sql)
        if prior is not None:  # a re-run: new rows under the same name, all or nothing
            try:
                done = self.replacement.replace(prior.name, sql, max_rows, stop=stop, rerun=Rerun(owner, title))
            except ReplacementFailed as e:
                return failure(e.error, e.raw_error, e.stopped)
            except RefreshInProgress as e:
                return failure(str(e))
            except TableExpired:
                return failure(f"The table {prior.name} expired during the run. Run the cell again.")
            t = done.table
            return _TableMade(t.name, t.columns, done.row_count, t.truncated_to)
        result = queries.run_query(self.telemetry_db, self.results_db, sql, self.timeout_s, max_rows, readable, title, stop)
        if not result["success"]:
            return failure(result["error"], result.get("raw_error"), bool(result.get("stopped")))
        name = result["result_table"]
        try:
            self.tables.register_for(owner, name, title, result["columns"], table_names.SQL_CELL_REQUEST,
                                     result.get("truncated_to"), sql, max_rows)
        except BaseException:
            queries.drop_table(self.results_db, name)
            raise
        return _TableMade(name, result["columns"], result["row_count"], result.get("truncated_to"))
