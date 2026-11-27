"""The Agent: builds each User's parts (Notebook storage, the Table Catalog, Table Replacement, the Run modules and the
Notebook module, `notebooks`, and the query runner, `queries`) and supervises the one MCP connection they share.
`Agent.for_user` gives what it does for one User (`UserSpace`), Refresh and row reads included.

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

from flood_agent import downloads, queries
from flood_agent.ai_cell_run import AiCellRuns, ClientFactory, ConnectionProvider
from flood_agent.catalog import Catalog, Table
from flood_agent.mcp_connection import McpConnection, Transport, stdio_transport
from flood_agent.mcp_server import ServerSettings
from flood_agent.model_choices import ModelChoices
from flood_agent.notebook import Notebooks
from flood_agent.notebook_store import NotebookStore
from flood_agent.public_keys import PublicKeys
from flood_agent.queries import QueryRunner
from flood_agent.semantic_layer import SemanticLayer
from flood_agent.sql_cell_run import SqlCellRuns
from flood_agent.table_replacement import RefreshInProgress, ReplacementFailed, TableReplacement
from flood_agent.users import UserDatabases, user_databases

log = logging.getLogger("uvicorn.error")  # the app's terminal log


class NotRefreshable(Exception):
    """The table has no stored query, so Refresh cannot run it."""


class RefreshFailed(Exception):
    """A Refresh did not complete; the message is plain text safe to show the user."""


@dataclass
class AgentConfig:
    """Built from `config.Settings`, which holds every default."""

    telemetry_db: Path
    users_dir: Path  # holds each User's own databases (`users.user_databases`)
    users: tuple[str, ...]  # the User names: the Agent builds one set of parts for each
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
    """One User's parts, over that User's databases. Notebook storage (`store`) is for the Agent's startup cleanup only;
    everything else uses the Notebook module."""

    databases: UserDatabases
    store: NotebookStore
    tables: Catalog
    replacement: TableReplacement
    sql_cells: SqlCellRuns
    ai_cells: AiCellRuns
    notebooks: Notebooks
    queries: QueryRunner
    public_keys: PublicKeys


def build_parts(config: AgentConfig, user: str, semantic_layer: SemanticLayer, client_for: ClientFactory,
                connection: ConnectionProvider, started: Callable[[], bool]) -> Parts:
    """Build Notebook storage, the Public Key store, the query runner, the Table Catalog, Table Replacement, the Run modules and the Notebook
    module for `user`, wired together over that User's databases (created if missing). `connection` and `started` are
    the MCP connection the AI Cell Runs use and whether it is up."""
    databases = user_databases(config.users_dir, user)
    store = NotebookStore(databases.app_db, config.clock)
    # The runner asks for the readable set only when it queries, by which time the Catalog below exists.
    runner = QueryRunner(config.telemetry_db, databases.results_db, config.query_timeout_s, lambda: tables.readable(),
                         lambda name: tables.recorded_sources(name), lambda name: tables.holds(name))
    tables = Catalog(
        databases.app_db, config.clock, config.result_ttl_ms, restricted_columns_by_table=semantic_layer.restricted_columns(),
        results_db=databases.results_db, on_rename=store.follow_rename,  # Displays follow a table's new name
        runner=runner,
    )
    replacement = TableReplacement(tables, runner)
    sql_cells = SqlCellRuns(store, tables, replacement, runner, config.clock)
    ai_cells = AiCellRuns(user, store, tables, semantic_layer, client_for, connection, started, config.choices.spec_of,
                          config.max_llm_steps, config.max_failed_attempts, config.clock)
    notebooks = Notebooks(store, tables, sql_cells, ai_cells, config.choices)
    return Parts(databases, store, tables, replacement, sql_cells, ai_cells, notebooks, runner,
                 PublicKeys(databases.app_db, config.clock))


class UserSpace:
    """What the Agent does for one User, over that User's parts alone: another User's Notebooks and tables are unknown
    here."""

    def __init__(self, config: AgentConfig, semantic_layer: SemanticLayer, parts: Parts):
        self.config, self.semantic_layer = config, semantic_layer
        self.databases = parts.databases
        self.tables, self.replacement, self.notebooks = parts.tables, parts.replacement, parts.notebooks
        self.queries, self.public_keys = parts.queries, parts.public_keys
        self._store, self._sql_cells, self._ai_cells = parts.store, parts.sql_cells, parts.ai_cells
        self._refreshing: set[tuple[str, str | int]] = set()  # ("count", id) being refreshed now (tables: see table_replacement)
        self._refreshing_lock = threading.Lock()

    def clean_up(self) -> None:
        """The startup cleanup: expired and orphaned tables, and Runs the app left running."""
        self.tables.sweep()
        # A Run still running now was cut off by the app being killed. One app process per User's databases, so
        # none of these is really in progress (eval runs and the caching smoke test use their own directory).
        for run_id in self._store.running_runs():
            self._store.finish_run(run_id, "failed", APP_STOPPED)

    async def stop_sql_cells(self) -> None:
        """Stop this User's running SQL Cells and wait for their queries; no more start."""
        await self._sql_cells.aclose()

    async def stop_ai_cells(self) -> None:
        """Stop this User's running AI Cells and wait for them; no more start."""
        await self._ai_cells.aclose()

    def fetch_rows(self, result_table: str, columns: list[str] | None = None, **page_args) -> dict:
        """Internal data path used by the row-JSON endpoint. Not an LLM tool.

        Adds per-column display formats from the Semantic Layer."""
        table = self.tables[result_table]
        data = self.queries.fetch_rows(table.name, columns, **page_args)
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
            result = self.queries.count(definition["sql"])
            if not result["success"]:
                raise RefreshFailed(self._refresh_error(label, result))
            computed_at_ms = self.config.clock()
            self.notebooks.update_count(display_id, result["count"], computed_at_ms)
            return {"value": result["count"], "computed_at_ms": computed_at_ms}

    def download(self, result_table: str, mode: downloads.Mode) -> downloads.Download:
        """Internal data path for downloading a whole Result or Saved Table as PSV. Not an LLM tool.

        An encrypted one is for this User's Public Key. KeyError if unknown, `TableExpired` if no longer live,
        `downloads.DownloadRefused` if the table may not be had in `mode`, `downloads.NoPublicKey` if there is no key
        to encrypt for."""
        key = self.public_keys.get() if mode == "encrypted" else None
        with self.tables.settled():  # so the rows read are the ones the restriction checked here is about
            table = self.tables[result_table]
            allowed = downloads.check(table, self.tables.restriction(table), mode, key)
            columns, rows = self.queries.all_rows(table.name)
        return downloads.psv_file(allowed, columns, rows)  # encrypting holds no replacement up

    def fetch_chart_rows(self, result_table: str) -> list[dict]:
        """Internal data path for the chart-rows endpoint (chart cap). Not an LLM tool."""
        return self.queries.chart_rows(self.tables[result_table].name, self.config.chart_row_cap)


class Agent:
    def __init__(self, config: AgentConfig, client_for: ClientFactory):
        self.config = config
        self.client_for = client_for
        self.semantic_layer = SemanticLayer(config.semantic_layer)
        self._users = {user: self._space(user) for user in config.users}
        self._connection: McpConnection | None = None
        self._restart_lock = asyncio.Lock()
        for space in self._users.values():
            space.clean_up()

    def _space(self, user: str) -> UserSpace:
        # `client_for` is looked up on each Run, so a client swapped in later is the one used.
        parts = build_parts(self.config, user, self.semantic_layer, lambda model: self.client_for(model),
                            self._ensure_connection, lambda: self._connection is not None)
        return UserSpace(self.config, self.semantic_layer, parts)

    def for_user(self, user: str) -> UserSpace:
        """`user`'s parts. KeyError for a name that is not one of the configured Users."""
        return self._users[user]

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
        for space in self._users.values():
            await space.stop_sql_cells()  # SQL Cell queries are stopped and waited for
        # AI Cell Runs before the connection (this also cancels a restart in progress), so none is left
        # talking to a closing connection.
        for space in self._users.values():
            await space.stop_ai_cells()
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
        # Static settings only. The User and the row cap travel in each call's `_meta`, not here
        # and not as tool arguments, so the LLM can't choose them.
        return ServerSettings(
            telemetry_db=self.config.telemetry_db,
            users_dir=self.config.users_dir,
            users=self.config.users,
            query_timeout_s=self.config.query_timeout_s,
            chart_row_cap=self.config.chart_row_cap,
            restricted_columns_by_table=self.semantic_layer.restricted_columns(),
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
