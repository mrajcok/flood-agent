"""SQL Cell Runs: everything a SQL Cell's Run does, without the LLM.

A Run takes the Cell's Draft and runs one of three things:
- a SHOW or DESCRIBE TABLE command, whose rows are kept with the Run
- a bare count, which gives a Count Display
- a query, which makes a Result Table, or for a re-run puts new rows under the latest Run's own table through
  table replacement (ADR 0006)

It records the Run in the NotebookStore: its status (and, if it failed, its value-free error), its saved result,
and the message AI Cells below are sent.
That message is frozen at this Run, and from a table it carries only Query Metadata, never rows (ADR 0001).
"""

import asyncio
import contextlib
import json
import logging
import threading
import time
from dataclasses import dataclass
from typing import AsyncIterator, Callable

from flood_agent import lineage, queries, table_names
from flood_agent.ai_cell_context import SHOW_CELL_LABEL, SQL_CELL_LABEL, STOPPED_BELOW
from flood_agent.catalog import Catalog, CellRun, Table, TableExpired
from flood_agent.cell import SqlCell
from flood_agent.events import CountEvent, DisplayEvent, Event, TurnEnd, TurnError
from flood_agent.notebook_store import COUNT_KIND, NotebookStore, RunStatus
from flood_agent.queries import QueryRunner
from flood_agent.show_commands import ShowFailed, run_show
from flood_agent.table_replacement import RefreshInProgress, ReplacementFailed, Rerun, TableReplacement

log = logging.getLogger("uvicorn.error")  # the app's terminal log

SHOW_ROWS_TO_AI = 100  # how many rows of a SHOW Cell's result an AI Cell below is sent
CRASHED = "The query could not be run."


# What the blocking part of a Run ended with. Each knows the Run's status, how it is saved with the Run (a stored
# format the page and saved Notebooks read: keep its keys), how it reads to the AI Cells below, and what it shows:
# the Displays saved with the Run and the events streamed before the end.

_Shown = tuple[list[tuple], list[Event]]


@dataclass
class _ShowRows:
    columns: list[str]
    rows: list[list]
    command: str
    status: RunStatus = "ok"
    error = None

    def saved(self) -> dict:
        return {"columns": self.columns, "rows": self.rows, "row_count": len(self.rows),
                "command": self.command}

    def detail(self) -> str:
        """The columns and up to SHOW_ROWS_TO_AI rows, with a note when rows were left out."""
        lines = [" | ".join(self.columns)]
        lines += [" | ".join("" if v is None else str(v) for v in r) for r in self.rows[:SHOW_ROWS_TO_AI]]
        if len(self.rows) > SHOW_ROWS_TO_AI:
            lines.append(f"(first {SHOW_ROWS_TO_AI} of {len(self.rows)} rows; the other rows were left out)")
        return "\n".join(lines)

    def shown(self, sql: str, clock: Callable[[], int]) -> _Shown:  # the rows are in the saved result
        return [], []


@dataclass
class _Count:
    value: int
    status: RunStatus = "ok"
    error = None

    def saved(self) -> dict:
        return {"count": self.value}

    def detail(self) -> str:
        return f"Count: {self.value}"

    def shown(self, sql: str, clock: Callable[[], int]) -> _Shown:
        computed, title = clock(), queries.shorten_sql(sql)
        display = (COUNT_KIND, None, {"title": title, "value": self.value, "sql": sql, "computed_at_ms": computed})
        return [display], [CountEvent(title, self.value, sql, computed)]


@dataclass
class _TableMade:
    name: str
    columns: list[dict]
    row_count: int
    truncated_to: int | None
    status: RunStatus = "ok"
    error = None

    def saved(self) -> dict:  # the columns are the table's, not the Run's
        return {"table": self.name, "row_count": self.row_count, "truncated_to": self.truncated_to}

    def detail(self) -> str:
        meta: dict = {"result_table": self.name, "columns": self.columns, "row_count": self.row_count}
        if self.truncated_to:
            meta["truncated_to"] = self.truncated_to
        return f"Query Metadata: {json.dumps(meta)}"

    def shown(self, sql: str, clock: Callable[[], int]) -> _Shown:
        names = [c["name"] for c in self.columns]
        return [("table", self.name, {"columns": names})], [DisplayEvent("table", self.name, names)]


def _refused(text: str) -> "_Failed":
    """A failure whose one message is both what the user sees and what the AI Cells below are sent."""
    return _Failed(text, text)


@dataclass
class _Failed:
    error: str  # value-free: stored on the Run, and what the AI Cells below are sent
    raw_error: str  # for the user, and logged
    status: RunStatus = "failed"

    def saved(self) -> dict:  # the value-free error is the Run's
        return {"raw_error": self.raw_error}

    def detail(self) -> str:
        return f"Failed: {self.error}"

    def shown(self, sql: str, clock: Callable[[], int]) -> _Shown:
        return [], [TurnError(self.raw_error, "sql cell failed")]


@dataclass
class _Stopped:
    status: RunStatus = "stopped"
    error = None

    def saved(self) -> dict:
        return {}

    def detail(self) -> str:
        return STOPPED_BELOW

    def shown(self, sql: str, clock: Callable[[], int]) -> _Shown:
        return [], []


_Outcome = _ShowRows | _Count | _TableMade | _Failed | _Stopped


class SqlCellRuns:
    """Runs SQL Cells and stops them. Built once by the Agent; the Notebook module keeps the rule that one Cell runs per
    Notebook at a time."""

    def __init__(self, store: NotebookStore, tables: Catalog, replacement: TableReplacement,
                 runner: QueryRunner, clock: Callable[[], int]):
        self._store, self.tables, self.replacement = store, tables, replacement
        self.runner, self.clock = runner, clock
        self._running: dict[str, tuple[threading.Event, asyncio.Future]] = {}  # SQL Cell id -> its query's stop flag and thread
        self._closed = False

    async def run(self, cell: SqlCell) -> AsyncIterator[Event]:
        """Run a SQL Cell with what its Draft would run as and stream its events: a table or Count Display, an
        error, then the end. Its Run replaces the Cell's earlier one. If the stream is abandoned (the client left,
        or the app is closing), the query is stopped and waited for, and the Run is recorded as stopped.
        RuntimeError after `aclose`."""
        if self._closed:
            raise RuntimeError("SQL Cell Runs are closed")
        cell_id, sql, table, statement = cell.id, cell.draft.sql, cell.draft.table, cell.statement
        settings = cell.run_settings
        max_rows, command = settings.max_rows, settings.command
        last = cell.run  # the table a re-run replaces: the latest Run's own Result Table, if still live; never a Saved Table
        prior = next((t for t in (self.tables.tables_of_run(cell_id, last.id) if last else []) if t.kind == "result"), None)
        run_id = self._store.start_run(cell_id, statement, settings.stored(), "")
        stop = threading.Event()
        work = asyncio.ensure_future(asyncio.to_thread(
            self._execute, sql, max_rows, stop, prior, CellRun(cell_id, run_id), command, table))
        self._running[cell_id] = (stop, work)
        started = time.monotonic()
        try:
            try:
                outcome: _Outcome = await asyncio.shield(work)  # a cancel reaches us, not the query's thread
            except Exception:
                log.exception("SQL Cell %s crashed", cell_id)
                outcome = _refused(CRASHED)
            except BaseException:  # abandoned: stop the query, wait for its thread, then let the cancel through
                stop.set()
                with contextlib.suppress(BaseException):
                    await work
                self._store.finish_run(run_id, "stopped")
                raise
        finally:
            self._running.pop(cell_id, None)
        elapsed_s = round(time.monotonic() - started, 3)

        label = f"{SHOW_CELL_LABEL} {statement}" if command else f"{SQL_CELL_LABEL} {sql}"
        displays, events = outcome.shown(sql, self.clock)
        # The message is stored once, so what AI Cells below are sent is frozen at this Run.
        self._store.add_step(run_id, [({"role": "user", "content": f"{label}\n{outcome.detail()}"}, displays)])
        self._store.save_result(run_id, outcome.saved() | {"elapsed_s": elapsed_s})
        self._store.finish_run(run_id, outcome.status, outcome.error)
        for event in events:
            yield event
        yield TurnEnd()

    def stop(self, cell_id: str) -> bool:
        """Stop a running SQL Cell now: its query's progress handler sees the flag and aborts it, and the Run is
        recorded as stopped. False if the Cell is not running here."""
        running = self._running.get(cell_id)
        if running is None:
            return False
        running[0].set()
        return True

    async def aclose(self) -> None:
        """Stop every running SQL Cell's query and wait for its thread, so none is left using the databases. No Run
        starts after this."""
        self._closed = True
        running = list(self._running.values())
        for stop, _ in running:
            stop.set()
        await asyncio.gather(*(work for _, work in running), return_exceptions=True)

    def _execute(self, sql: str, max_rows: int, stop: threading.Event, prior: Table | None, owner: CellRun,
                 command: str, table: str) -> _Outcome:
        """The blocking part of a Run (it runs in a thread)."""
        if command:  # SHOW / DESCRIBE: no table, no row cap; the rows are kept with the Run
            try:
                names, rows = run_show(command, table, self.runner, self.tables.listing())
            except ShowFailed as e:
                log.warning("SQL Cell %s failed: %s", owner.cell_id, e)
                return _refused(str(e))
            return _ShowRows(names, rows, command)

        def failure(error: str, raw_error: str | None = None, stopped: bool = False) -> _Outcome:
            if stopped:
                return _Stopped()
            raw = raw_error or error
            log.warning("SQL Cell %s failed: %s", owner.cell_id, raw)
            return _Failed(error, raw)

        if queries.looks_like_count(sql):
            counted = self.runner.count(sql, stop)
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
        result = self.runner.run(sql, max_rows, title, stop)
        if not result["success"]:
            return failure(result["error"], result.get("raw_error"), bool(result.get("stopped")))
        name = result["result_table"]
        try:
            self.tables.register_for(owner, name, title, result["columns"], table_names.SQL_CELL_REQUEST,
                                     result.get("truncated_to"), sql, max_rows,
                                     lineage.restriction(result["lineage"]))
        except BaseException:
            self.runner.drop(name)
            raise
        return _TableMade(name, result["columns"], result["row_count"], result.get("truncated_to"))
