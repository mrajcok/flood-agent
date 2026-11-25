"""The Agent: builds the app's parts (Notebook storage, the Table Catalog, Table Replacement, the Run modules and the
Notebook module, `notebooks`), supervises the MCP connection, and does Refresh and row reads.

The LLM client is injected.
"""

import asyncio
import logging
import sys
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterator

from mcp import StdioServerParameters

from flood_agent import queries
from flood_agent.ai_cell_run import AiCellRuns, ClientFactory, ConnectionProvider
from flood_agent.catalog import Catalog, Table
from flood_agent.mcp_connection import McpConnection, Transport, stdio_transport
from flood_agent.mcp_server import ServerSettings
from flood_agent.model_choices import ModelChoices
from flood_agent.notebook import Notebooks
from flood_agent.notebook_store import NotebookStore
from flood_agent.semantic_layer import SemanticLayer
from flood_agent.sql_cell_run import SqlCellRuns
from flood_agent.table_replacement import RefreshInProgress, ReplacementFailed, TableReplacement
from flood_agent.upgrade import drop_pre_notebook_data

log = logging.getLogger("uvicorn.error")  # the app's terminal log


class NotRefreshable(Exception):
    """The table has no stored query, so Refresh cannot run it."""


class RefreshFailed(Exception):
    """A Refresh did not complete; the message is plain text safe to show the user."""


@dataclass
class AgentConfig:
    """Built from `config.Settings`, which holds every default."""

    telemetry_db: Path
    results_db: Path
    app_db: Path
    semantic_layer: Path
    choices: ModelChoices  # the models that can be used here and the default model
    result_ttl_ms: int  # Result Tables expire this long after they are made
    query_timeout_s: float
    chart_row_cap: int
    max_failed_attempts: int
    max_llm_steps: int
    clock: Callable[[], int] = lambda: int(time.time() * 1000)  # epoch ms; injectable for tests
    fixed_now_ms: int | None = None  # pins SQL now() in the MCP server process (evals); None is the wall clock


APP_STOPPED = "The app stopped during this Run."
"""The value-free reason a Run left running by a killed app is failed with when the app next starts."""


@dataclass(frozen=True)
class Parts:
    """The app's parts over one app database. Notebook storage (`store`) is for the Agent's startup cleanup only;
    everything else uses the Notebook module."""

    store: NotebookStore
    tables: Catalog
    replacement: TableReplacement
    sql_cells: SqlCellRuns
    ai_cells: AiCellRuns
    notebooks: Notebooks


def build_parts(config: AgentConfig, semantic_layer: SemanticLayer, client_for: ClientFactory,
                connection: ConnectionProvider, started: Callable[[], bool]) -> Parts:
    """Build Notebook storage, the Table Catalog, Table Replacement, the Run modules and the Notebook module, wired
    together. `connection` and `started` are the MCP connection the AI Cell Runs use and whether it is up."""
    store = NotebookStore(config.app_db, config.clock)
    tables = Catalog(
        config.app_db, config.clock, config.result_ttl_ms, telemetry_tables=semantic_layer.table_names(),
        results_db=config.results_db, on_rename=store.follow_rename,  # Displays follow a table's new name
    )
    replacement = TableReplacement(tables, config.telemetry_db, config.results_db, config.query_timeout_s)
    sql_cells = SqlCellRuns(store, tables, replacement, config.telemetry_db, config.results_db,
                            config.query_timeout_s, config.clock)
    ai_cells = AiCellRuns(store, tables, semantic_layer, client_for, connection, started, config.choices.spec_of,
                          config.max_llm_steps, config.max_failed_attempts, config.clock)
    return Parts(store, tables, replacement, sql_cells, ai_cells, Notebooks(store, tables, sql_cells, ai_cells,
                                                                             config.choices))


class Agent:
    def __init__(self, config: AgentConfig, client_for: ClientFactory):
        self.config = config
        self.client_for = client_for
        self.semantic_layer = SemanticLayer(config.semantic_layer)
        drop_pre_notebook_data(config.app_db, config.results_db)  # before anything creates the new schema
        # `client_for` is looked up on each Run, so a client swapped in later is the one used.
        parts = build_parts(config, self.semantic_layer, lambda model: self.client_for(model), self._ensure_connection,
                            lambda: self._connection is not None)
        self.tables, self.replacement, self.notebooks = parts.tables, parts.replacement, parts.notebooks
        self._sql_cells, self._ai_cells = parts.sql_cells, parts.ai_cells
        self._connection: McpConnection | None = None
        self._restart_lock = asyncio.Lock()
        self._refreshing: set[tuple[str, str | int]] = set()  # ("count", id) being refreshed now (tables: see table_replacement)
        self._refreshing_lock = threading.Lock()
        self.tables.sweep()
        # A Run still running now was cut off by the app being killed. One app process per app database, so
        # none of these is really in progress (eval runs and the caching smoke test use their own app database).
        for run_id in parts.store.running_runs():
            parts.store.finish_run(run_id, "failed", APP_STOPPED)

    async def start(self) -> None:
        """Launch the long-lived MCP server. Needed before `run_cell`, not for row fetches
        or PSV export."""
        if self._connection is not None:
            raise RuntimeError("Agent already started")
        connection = McpConnection(self.transport())
        await connection.start()
        self._connection = connection

    @property
    def mcp_server_alive(self) -> bool:
        """Whether the MCP connection is up: False before `start`, after `aclose`, or once the server has died
        (the next AI Cell Run replaces it)."""
        return self._connection is not None and self._connection.alive

    async def _ensure_connection(self) -> McpConnection:
        """The live connection; a dead one (process exited or session broken) is replaced first."""
        async with self._restart_lock:
            connection = self._connection
            if connection is None:
                raise RuntimeError("Agent not started")
            if not connection.alive:
                replacement = McpConnection(self.transport())
                await connection.aclose()  # reap the dead one
                await replacement.start()
                self._connection = replacement
                connection = replacement
            return connection

    async def aclose(self) -> None:
        """Stop running Cells and the MCP server, and reap its process."""
        await self._sql_cells.aclose()  # SQL Cell queries are stopped and waited for
        # AI Cell Runs before the connection (this also cancels a restart in progress), so none is left
        # talking to a closing connection.
        await self._ai_cells.aclose()
        async with self._restart_lock:
            connection, self._connection = self._connection, None
            if connection is not None:
                await connection.aclose()

    async def __aenter__(self) -> "Agent":
        await self.start()
        return self

    async def __aexit__(self, *exc) -> None:
        await self.aclose()

    def server_settings(self) -> ServerSettings:
        """What the MCP server is launched with."""
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
            command=sys.executable, args=["-m", "flood_agent.mcp_server"], env=self.server_settings().to_env()
        )

    def transport(self) -> Transport:
        """How each new MCP connection reaches its server: a stdio subprocess. The seam where tests run the server
        in-process instead."""
        return stdio_transport(self._server_params())

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
