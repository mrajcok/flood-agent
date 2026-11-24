"""The Agent: the Notebook operations, Refresh, and MCP connection supervision. It runs one Cell per Notebook at a
time, handing each Run to the SQL Cell Run or AI Cell Run module, and streams its events.

The HTTP/SSE layer is a thin adapter over `Agent.run_cell`. The LLM client is injected.
"""

import asyncio
import logging
import sys
import threading
import time
from contextlib import aclosing, contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import AsyncGenerator, AsyncIterator, Callable, Iterator, Literal, cast

from mcp import StdioServerParameters

from flood_agent import ai_cell_context, queries, table_names
from flood_agent.ai_cell_run import (  # noqa: F401 (the messages and resolve_chart_data are re-exported)
    GIVE_UP_MESSAGE,
    STEP_LIMIT_MESSAGE,
    AiCellRuns,
    ClientFactory,
    resolve_chart_data,
)
from flood_agent.catalog import Catalog, Table, canonical
from flood_agent.cell_kinds import AI, MARKDOWN, SQL
from flood_agent.events import (  # noqa: F401 (re-exported: callers import the event types from here)
    CountEvent,
    DisplayEvent,
    Event,
    FlagEvent,
    LlmCallStarted,
    TextDelta,
    TraceEntry,
    TraceLive,
    TraceReady,
    TurnEnd,
    TurnError,
)
from flood_agent.llm import ModelSpec
from flood_agent.mcp_connection import McpConnection, Transport, stdio_transport
from flood_agent.mcp_server import ServerSettings
from flood_agent.notebooks import DEFAULT_MAX_ROWS, NotebookStore
from flood_agent.semantic_layer import SemanticLayer
from flood_agent.sql_cell_run import SqlCellRuns
from flood_agent.table_replacement import RefreshInProgress, ReplacementFailed, TableReplacement
from flood_agent.upgrade import drop_pre_notebook_data

log = logging.getLogger("uvicorn.error")  # the app's terminal log


class NotRefreshable(Exception):
    """The table has no stored query, so Refresh cannot run it."""


class NotebookBusy(Exception):
    """A cell in this notebook is already running; only one runs at a time."""


class RefreshFailed(Exception):
    """A Refresh did not complete; the message is plain text safe to show the user."""


# Whether deleting a Notebook or Cell also drops its live Result Tables (those still named
# rt_*). Its Saved Tables are always kept (ADR 0004).
TablesChoice = Literal["keep", "delete_rt"]


@dataclass
class AgentConfig:
    """Built from `config.Settings`, which holds every default."""

    telemetry_db: Path
    results_db: Path
    app_db: Path
    semantic_layer: Path
    model: str  # the default model: runs a turn that names none
    models: tuple[ModelSpec, ...]  # the curated list; window_of looks a model's window up here
    result_ttl_ms: int  # Result Tables expire this long after they are made
    query_timeout_s: float
    chart_row_cap: int
    max_failed_attempts: int
    max_llm_steps: int
    clock: Callable[[], int] = lambda: int(time.time() * 1000)  # epoch ms; injectable for tests
    fixed_now_ms: int | None = None  # pins SQL now() in the MCP server process (evals); None is the wall clock

    def spec_of(self, model_id: str) -> ModelSpec | None:
        """The curated model with this id; None for an id outside the list (NO_MODEL included)."""
        return next((m for m in self.models if m.id == model_id), None)

    def window_of(self, model: str) -> int:
        """The context window of a curated model; 0 for an id outside the list (NO_MODEL: no context window)."""
        spec = self.spec_of(model)
        return spec.context_window if spec else 0


APP_STOPPED = "The app stopped during this Run."
"""The value-free reason a Run left running by a killed app is failed with when the app next starts."""

NO_MODEL = "no-model"
"""The Model Choice id for Fake Mode: a prompt sent with it is answered by the scripted stand-in."""


class Agent:
    def __init__(self, config: AgentConfig, client_for: ClientFactory):
        self.config = config
        self.client_for = client_for
        self.semantic_layer = SemanticLayer(config.semantic_layer)
        drop_pre_notebook_data(config.app_db, config.results_db)  # before anything creates the new schema
        self.notebooks = NotebookStore(config.app_db, config.clock)
        self.tables = Catalog(
            config.app_db, config.clock, config.result_ttl_ms, telemetry_tables=self.semantic_layer.table_names(),
            results_db=config.results_db, on_rename=self._follow_rename,
        )
        self.replacement = TableReplacement(self.tables, config.telemetry_db, config.results_db, config.query_timeout_s)
        self.sql_cells = SqlCellRuns(self.notebooks, self.tables, self.replacement, config.telemetry_db,
                                     config.results_db, config.query_timeout_s, config.clock)
        # `client_for` is looked up on each Run, so a client swapped in later is the one used.
        self.ai_cells = AiCellRuns(self.notebooks, self.tables, self.semantic_layer, lambda model: self.client_for(model),
                                   self._ensure_connection, config.model, config.spec_of, config.max_llm_steps,
                                   config.max_failed_attempts, config.clock)
        self._connection: McpConnection | None = None
        self._restart_lock = asyncio.Lock()
        self._running_cells: dict[str, str] = {}  # notebook id -> the cell running in it now
        self._refreshing: set[tuple[str, str | int]] = set()  # ("count", id) being refreshed now (tables: see table_replacement)
        self._refreshing_lock = threading.Lock()
        self.tables.sweep()
        # A Run still running now was cut off by the app being killed. One app process per app database, so
        # none of these is really in progress (eval runs and the caching smoke test use their own app database).
        for run_id in self.notebooks.running_runs():
            self.notebooks.finish_run(run_id, "failed", APP_STOPPED)

    async def start(self) -> None:
        """Launch the long-lived MCP server. Needed before `run_cell`, not for row fetches
        or PSV export."""
        if self._connection is not None:
            raise RuntimeError("Agent already started")
        connection = McpConnection(self._transport())
        await connection.start()
        self._connection = connection

    async def _ensure_connection(self) -> McpConnection:
        """The live connection; a dead one (process exited or session broken) is replaced first."""
        async with self._restart_lock:
            connection = self._connection
            if connection is None:
                raise RuntimeError("Agent not started")
            if not connection.alive:
                replacement = McpConnection(self._transport())
                await connection.aclose()  # reap the dead one
                await replacement.start()
                self._connection = replacement
                connection = replacement
            return connection

    async def aclose(self) -> None:
        """Stop running Cells and the MCP server, and reap its process."""
        await self.sql_cells.aclose()  # SQL Cell queries are stopped and waited for
        # AI Cell Runs before the connection (this also cancels a restart in progress), so none is left
        # talking to a closing connection.
        await self.ai_cells.aclose()
        async with self._restart_lock:
            connection, self._connection = self._connection, None
            if connection is not None:
                await connection.aclose()

    async def __aenter__(self) -> "Agent":
        await self.start()
        return self

    async def __aexit__(self, *exc) -> None:
        await self.aclose()

    def _server_settings(self) -> ServerSettings:
        # Static settings only. The row cap travels in each call's `_meta`, not here
        # and not as a tool argument, so the LLM can't choose it.
        return ServerSettings(
            telemetry_db=self.config.telemetry_db,
            results_db=self.config.results_db,
            app_db=self.config.app_db,
            query_timeout_s=self.config.query_timeout_s,
            chart_row_cap=self.config.chart_row_cap,
            telemetry_tables=tuple(self.semantic_layer.table_names()),
            fixed_now_ms=self.config.fixed_now_ms,
        )

    def _server_params(self) -> StdioServerParameters:
        return StdioServerParameters(
            command=sys.executable, args=["-m", "flood_agent.mcp_server"], env=self._server_settings().to_env()
        )

    def _transport(self) -> Transport:
        """How a new connection reaches its server: a stdio subprocess (tests swap in an in-process one)."""
        return stdio_transport(self._server_params())

    # --- Notebooks (AI Cells only so far) ---------------------------------------------------------------

    def create_notebook(self, title: str | None = None) -> str:
        """Persist a new, empty Notebook ("Notebook N" by default) and return its id."""
        self.tables.sweep()
        return self.notebooks.create_notebook(title)

    def add_ai_cell(
        self, notebook_id: str, prompt: str = "", model: str | None = None, effort: str | None = None,
        max_rows: int = DEFAULT_MAX_ROWS, after: str | None = None, before: str | None = None,
    ) -> str:
        """Add an AI Cell (after cell `after`, or at the end) whose Draft holds its prompt and settings."""
        return self.notebooks.add_ai_cell(
            notebook_id, {"prompt": prompt, "model": model, "effort": effort, "max_rows": max_rows}, after, before)

    def add_sql_cell(
        self, notebook_id: str, sql: str = "", max_rows: int = DEFAULT_MAX_ROWS, after: str | None = None,
        before: str | None = None, command: str = "", table: str = "",
    ) -> str:
        """Add a SQL Cell (after cell `after`, or at the end) whose Draft holds its SQL and row cap. A `command`
        (a SHOW statement or DESCRIBE TABLE, with its `table`) makes it a SHOW Cell: the SQL is not used."""
        return self.notebooks.add_cell(
            notebook_id, SQL, {"sql": sql, "max_rows": max_rows, "command": command, "table": table}, after, before)

    def _follow_rename(self, con, old: str, new: str) -> None:
        """Every Display, in a Run or a Markdown Cell, follows a table's new name."""
        self.notebooks.follow_rename(con, old, new)

    def _copyable_run(self, notebook_id: str, cell_id: str) -> tuple[str, list[dict]]:
        """An AI Cell's latest answer as a Markdown Cell would hold it: the text and the embedded Displays (a
        table or chart of a table that is still live, each with its own definition). Count Displays are not
        embedded: their number is already in the text. ValueError if the cell has not run."""
        cell = self._idle_cell(notebook_id, cell_id)
        if cell["kind"] != AI or cell["run"] is None:
            raise ValueError("Only an AI Cell that has run can be copied to a Markdown Cell.")
        texts: list[str] = []
        displays: list[dict] = []
        for item in self.notebooks.transcript(cell["run"]["id"]):
            if item["type"] == "message":
                m = item["message"]
                if m["role"] == "assistant" and m.get("content"):
                    text = ai_cell_context.without_stop_note(m["content"])
                    if text:
                        texts.append(text)
            elif item["kind"] in ("table", "chart") and item["result_table"] in self.tables:
                d = {"kind": item["kind"], "table": item["result_table"]}
                if item["kind"] == "chart":
                    d["spec"] = item["spec"]
                displays.append(d)
        return "\n\n".join(texts), displays

    def markdown_copy_tables(self, notebook_id: str, cell_id: str) -> list[str]:
        """The `rt_` tables a Copy to Markdown Cell would embed, which the user may save first (so the copy does
        not expire with them)."""
        _, displays = self._copyable_run(notebook_id, cell_id)
        return list(dict.fromkeys(d["table"] for d in displays if table_names.is_result_table_name(d["table"])))

    def copy_to_markdown_cell(self, notebook_id: str, cell_id: str, save: dict[str, str] | None = None) -> str:
        """Insert a Markdown Cell below an AI Cell holding its answer text and one embedded Display per table or
        chart, and return its id. Send to AI starts unticked. `save` maps `rt_` tables of the answer to the names
        to rename them to (Saved Tables); every name is checked first, so a ValueError changes nothing."""
        save = save or {}
        usable = self.markdown_copy_tables(notebook_id, cell_id)
        if not set(save) <= set(usable):
            raise ValueError("Only the answer's rt_ tables can be saved.")
        for new in save.values():
            self.tables.check_new_name(new)
        if len({canonical(n) for n in save.values()}) != len(save):
            raise ValueError("Each saved table needs its own name.")
        for old, new in save.items():
            self.tables.update(old, new_name=new)
        text, displays = self._copyable_run(notebook_id, cell_id)
        return self.notebooks.add_cell(
            notebook_id, MARKDOWN, {"text": text, "send_to_ai": False, "displays": displays}, after=cell_id)

    def add_markdown_cell(self, notebook_id: str, text: str = "", send_to_ai: bool = False,
                          after: str | None = None, before: str | None = None) -> str:
        """Add a Markdown Cell. Its text reaches the AI Cells below only while `send_to_ai` is ticked."""
        return self.notebooks.add_cell(notebook_id, MARKDOWN, {"text": text, "send_to_ai": send_to_ai}, after, before)

    def _idle_cell(self, notebook_id: str, cell_id: str) -> dict:
        """The cell, for an operation that cannot touch a running one: KeyError if it is not in the notebook,
        `NotebookBusy` if it is running."""
        cell = self.notebooks.cell(cell_id)
        if cell["notebook_id"] != notebook_id:
            raise KeyError(cell_id)
        if self.running_cell(notebook_id) == cell_id:
            raise NotebookBusy("This cell is running; stop it first")
        return cell

    def rename_notebook(self, notebook_id: str, title: str) -> None:
        """Retitle a notebook (at most 60 characters). ValueError if the title is blank, KeyError if unknown."""
        title = title.strip()[:60]
        if not title:
            raise ValueError("a notebook name cannot be blank")
        self.notebooks.rename_notebook(notebook_id, title)

    def _idle_notebook(self, notebook_id: str) -> list[dict]:
        """The notebook's cells, for an operation that cannot touch a running one: KeyError if unknown,
        `NotebookBusy` if a cell is running."""
        if not self.notebooks.exists(notebook_id):
            raise KeyError(notebook_id)
        if self.running_cell(notebook_id):
            raise NotebookBusy("A cell in this notebook is running; stop it first")
        return self.notebooks.cells(notebook_id)

    def notebook_delete_preview(self, notebook_id: str) -> dict:
        """Counts for the delete dialog: the live Result Tables `delete_rt` would drop (any cell's, any Run's)
        and the live Saved Tables that are always kept."""
        tables = [t for c in self._idle_notebook(notebook_id) for t in self.tables.tables_of_cell(c["id"])]
        return {"result": sum(t.kind == "result" for t in tables), "saved": sum(t.kind == "saved" for t in tables)}

    def delete_notebook(self, notebook_id: str, tables: TablesChoice = "keep") -> None:
        """Delete a notebook with its cells. `tables` (keep or delete_rt) says whether to drop the `rt_` tables its
        Runs made; Saved Tables are always kept. The tables go first, so a failure leaves the notebook in place."""
        cells = self._idle_notebook(notebook_id)
        if tables == "delete_rt":
            for cell in cells:
                self.tables.drop_of_cell(cell["id"])
        self.notebooks.delete_notebook(notebook_id)

    def move_cell(self, notebook_id: str, cell_id: str, direction: Literal["up", "down"]) -> None:
        """Swap a cell with the one above or below it (nothing at either end). Staleness follows from the new order."""
        self._idle_cell(notebook_id, cell_id)
        self.notebooks.move_cell(cell_id, direction)

    def copy_cell(self, notebook_id: str, cell_id: str) -> str:
        """Insert a copy of a cell's Draft just below it and return its id: an AI Cell's prompt and settings, a SQL
        Cell's SQL and row cap, a Markdown Cell's text and Send to AI. The copy has no Run."""
        cell = self._idle_cell(notebook_id, cell_id)
        return self.notebooks.add_cell(notebook_id, cell["kind"], dict(cell["draft"]), after=cell_id)

    def cell_delete_preview(self, notebook_id: str, cell_id: str) -> dict:
        """Counts for the delete dialog: the live Result Tables `delete_rt` would drop (any Run's) and the live
        Saved Tables that are always kept."""
        self._idle_cell(notebook_id, cell_id)
        tables = self.tables.tables_of_cell(cell_id)
        return {"result": sum(t.kind == "result" for t in tables), "saved": sum(t.kind == "saved" for t in tables)}

    def delete_cell(self, notebook_id: str, cell_id: str, tables: TablesChoice = "keep") -> None:
        """Delete a cell. `tables` (keep or delete_rt) says whether to drop the `rt_` tables its Runs made; kept
        ones expire per their Retention. Saved Tables are always kept. The tables go first, so a failure leaves
        the cell in place and the delete can be retried."""
        self._idle_cell(notebook_id, cell_id)
        if tables == "delete_rt":
            self.tables.drop_of_cell(cell_id)
        self.notebooks.delete_cell(cell_id)

    def delete_earlier_tables(self, notebook_id: str, cell_id: str) -> list[str]:
        """Drop the `rt_` tables of the cell's earlier Runs (never Saved Tables, never its latest Run's) and return
        their names. `NotebookBusy` if the cell is running."""
        cell = self._idle_cell(notebook_id, cell_id)
        if cell["run"] is None:
            return []
        return self.tables.drop_earlier_of_cell(cell_id, cell["run"]["id"])

    async def run_cell(self, notebook_id: str, cell_id: str) -> AsyncIterator[Event]:
        """Run a cell, with what its Draft holds, and stream its events. A SQL Cell runs without the LLM, through
        the SQL Cell Run module, and needs no `start()`; an AI Cell runs through the AI Cell Run module, and needs
        `start()` first. Its Run replaces the cell's earlier one. `NotebookBusy` if a cell of this notebook is
        already running, KeyError if the cell is not in the notebook."""
        cell = self.notebooks.cell(cell_id)
        if cell["notebook_id"] != notebook_id:
            raise KeyError(cell_id)
        if cell["kind"] != SQL and self._connection is None:
            raise RuntimeError("Agent not started: call `await agent.start()` or use `async with agent`")
        if notebook_id in self._running_cells:
            raise NotebookBusy("A cell in this notebook is already running.")
        self._running_cells[notebook_id] = cell_id
        runs = self.sql_cells if cell["kind"] == SQL else self.ai_cells
        try:
            # Closed with this stream, so that an abandoned stream (the page was closed) stops the Run now.
            async with aclosing(cast(AsyncGenerator, runs.run(cell))) as events:
                async for item in events:
                    yield item
        finally:
            if self._running_cells.get(notebook_id) == cell_id:
                del self._running_cells[notebook_id]

    def stale_cells(self, notebook_id: str) -> set[str]:
        """The ids of the notebook's AI Cells that are Stale (see `ai_cell_context.stale_cells`)."""
        return ai_cell_context.stale_cells(self.notebooks.cells(notebook_id))

    def running_cell(self, notebook_id: str) -> str | None:
        """The id of the cell running in this notebook now, or None."""
        return self._running_cells.get(notebook_id)

    def stop_cell(self, cell_id: str) -> bool:
        """Stop a running cell now. What it had streamed is kept and its Run is recorded as stopped.
        False if the cell is not running."""
        return self.sql_cells.stop(cell_id) or self.ai_cells.stop(cell_id)

    def fetch_rows(self, result_table: str, columns: list[str] | None = None, **page_args) -> dict:
        """Internal data path used by the row-JSON endpoint. Not an LLM tool.

        Adds per-column display formats from the Semantic Layer."""
        table = self.tables[result_table]
        data = queries.fetch_rows(self.config.results_db, table.name, columns, **page_args)
        data["formats"] = self.semantic_layer.column_formats(data["columns"])
        data["truncated_to"] = table.truncated_to
        data.update(table.to_json())
        return data

    def refresh_table(self, name: str) -> Table:
        """Refresh a live Result or Saved Table: run its stored query again, without the LLM, and replace
        it under the same name (ADR 0006), through table replacement: any failure leaves the table, its rows and
        its catalog row as they were.

        KeyError if unknown, `TableExpired` if no longer live, `NotRefreshable` if it has no stored query,
        `RefreshInProgress` if it is already being refreshed, `RefreshFailed` (with a plain message) if the
        query is rejected or fails. A plain call, not an MCP tool: no rows reach the LLM (ADR 0001)."""
        table = self.tables[name]
        if table.sql is None or table.max_rows is None:
            raise NotRefreshable(f"{table.name} has no stored query, so it cannot be refreshed.")
        try:
            return self.replacement.replace(table.name, table.sql, table.max_rows).table
        except ReplacementFailed as e:
            raise RefreshFailed(self._refresh_error(table.name, {"error": e.error, "raw_error": e.raw_error})) from e

    @contextmanager
    def _refresh_guard(self, key: tuple[str, str | int], label: str) -> Iterator[None]:
        """Allow one Refresh at a time per `key`; `RefreshInProgress` (naming `label`) for a second."""
        with self._refreshing_lock:
            if key in self._refreshing:
                raise RefreshInProgress(f"{label} is already being refreshed.")
            self._refreshing.add(key)
        try:
            yield
        finally:
            with self._refreshing_lock:
                self._refreshing.discard(key)

    @staticmethod
    def _refresh_error(what: str, result: dict) -> str:
        """Log a failed Refresh query and return the plain message to show: the sanitised error the LLM path
        uses, with a table that has expired since put in words."""
        log.warning("refresh of %s failed: %s", what, result.get("raw_error") or result["error"])
        error = result["error"]
        if error == queries.ACCESS_DENIED or "no such table" in error:  # a table it read has expired
            error = "A table this query reads has expired or been deleted. Ask again to regenerate it."
        return error

    def refresh_count(self, display_id: int) -> dict:
        """Refresh a Count Display: run its stored count query again, without the LLM, under the same checks
        and timeout as the original, and overwrite its number and "as of" time. Returns the new
        {"value", "computed_at_ms"}. KeyError if unknown, `RefreshInProgress` if already refreshing,
        `RefreshFailed` (plain message) if the query fails, in which case the old number is kept.
        A Refresh is never a Run."""
        definition = self.notebooks.count_display(display_id)
        if definition is None:
            raise KeyError(display_id)
        label = f'Count Display "{definition["title"]}"'
        with self._refresh_guard(("count", display_id), label):
            result = queries.run_count_query(
                self.config.telemetry_db, self.config.results_db, definition["sql"], self.config.query_timeout_s,
                self.tables.readable(),
            )
            if not result["success"]:
                raise RefreshFailed(self._refresh_error(label, result))
            computed_at_ms = self.config.clock()
            self.notebooks.update_count(display_id, result["count"], computed_at_ms)
            return {"value": result["count"], "computed_at_ms": computed_at_ms}

    def iter_all_rows(self, result_table: str):
        """Internal data path for PSV export of the full Result Table. Not an LLM tool."""
        return queries.iter_all_rows(self.config.results_db, self.tables[result_table].name)

    def fetch_chart_rows(self, result_table: str) -> list[dict]:
        """Internal data path for the chart-rows endpoint (chart cap). Not an LLM tool."""
        return queries.fetch_records(self.config.results_db, self.tables[result_table].name, self.config.chart_row_cap)
