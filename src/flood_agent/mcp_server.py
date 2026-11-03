"""MCP server. Exposes exactly the LLM-facing tools.

No tool here returns rows: `run_query` returns Query Metadata, `run_count_query` returns a
single Count (the ADR 0001 exception), `show_table` records a Display definition. Rows reach the
browser only through the Agent's internal data path.

`create_server(settings)` builds the server from static `ServerSettings`. In production it runs
as a stdio subprocess (`python -m flood_agent.mcp_server`) and reads those settings from the
launch environment the Agent passes it: TELEMETRY_DB, RESULTS_DB, APP_DB, QUERY_TIMEOUT_S,
and CHART_ROW_CAP. There are no fallbacks. Most tests run it in-process
(`tests/conftest.py`).

The Conversation id that limits which Result Tables a call may use arrives with every tool call in
the MCP request `_meta` (key `flood_agent/conversation_id`), set by the Agent. It is not a tool
argument, so the LLM never sees it. A missing or empty id means no Result Tables are owned.
"""

import functools
import json
import os
import sqlite3
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

import anyio.to_thread
from mcp.server.fastmcp import Context, FastMCP

from flood_agent import queries
from flood_agent.mcp_protocol import META_CONVERSATION_ID, META_MAX_RESULT_ROWS


@dataclass(frozen=True)
class ServerSettings:
    """The server's static launch settings. `app_db` None means no Result Tables are owned."""

    telemetry_db: Path
    results_db: Path
    app_db: Path | None
    query_timeout_s: float
    chart_row_cap: int

    def to_env(self) -> dict[str, str]:
        env = {
            "TELEMETRY_DB": str(self.telemetry_db),
            "RESULTS_DB": str(self.results_db),
            "QUERY_TIMEOUT_S": str(self.query_timeout_s),
            "CHART_ROW_CAP": str(self.chart_row_cap),
        }
        if self.app_db is not None:
            env["APP_DB"] = str(self.app_db)
        return env

    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> "ServerSettings":
        return cls(
            telemetry_db=Path(env["TELEMETRY_DB"]),
            results_db=Path(env["RESULTS_DB"]),
            app_db=Path(env["APP_DB"]) if env.get("APP_DB") else None,
            query_timeout_s=float(env["QUERY_TIMEOUT_S"]),
            chart_row_cap=int(env["CHART_ROW_CAP"]),
        )


def _in_worker_thread(fn):
    """Run a sync tool body in a worker thread. FastMCP calls sync tools directly on its event
    loop, so one slow `run_query` would stall every other Conversation's calls (ADR 0002)."""

    @functools.wraps(fn)  # keeps the signature, so FastMCP builds the same input schema
    async def wrapper(*args, **kwargs):
        return await anyio.to_thread.run_sync(functools.partial(fn, *args, **kwargs))

    return wrapper


def _conversation_id(ctx: Context) -> str | None:
    meta = ctx.request_context.meta
    value = getattr(meta, META_CONVERSATION_ID, None) if meta else None
    return value if isinstance(value, str) and value else None


def _max_result_rows(ctx: Context) -> int:
    """This turn's row cap from request `_meta`. There is no default: a call without a valid cap fails."""
    meta = ctx.request_context.meta
    value = getattr(meta, META_MAX_RESULT_ROWS, None) if meta else None
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise ValueError("The request must carry a max_result_rows of at least 1 in its _meta.")
    return value


def _live_result_tables(app_db: Path | None, conversation_id: str | None = None) -> set[str]:
    """Live Result Tables read from the app database with a read-only query (no DDL, no write
    lock): every Conversation's, or only `conversation_id`'s when given."""
    if not app_db:
        return set()
    sql, args = "SELECT name FROM result_tables WHERE expired=0", ()
    if conversation_id is not None:
        sql, args = sql + " AND conversation_id=?", (conversation_id,)
    con = sqlite3.connect(f"file:{app_db}?mode=ro", uri=True, timeout=5)
    try:
        return {r[0] for r in con.execute(sql, args)}
    finally:
        con.close()


def _allowed_tables(app_db: Path | None, ctx: Context) -> set[str]:
    """The calling Conversation's own live Result Tables (what its Displays may show). Empty if
    the Conversation id or APP_DB is missing."""
    conversation_id = _conversation_id(ctx)
    return _live_result_tables(app_db, conversation_id) if conversation_id else set()


def _lookup_tables(app_db: Path | None) -> set[str]:
    """Registered Lookup Tables (registration, not the `lookup_` prefix, decides), read-only."""
    if not app_db:
        return set()
    con = sqlite3.connect(f"file:{app_db}?mode=ro", uri=True, timeout=5)
    try:
        return {r[0] for r in con.execute("SELECT name FROM lookup_tables")}
    finally:
        con.close()


def _readable_tables(app_db: Path | None) -> set[str]:
    """Every table a query may read (ADR 0003): all live Result Tables, from any Conversation,
    and every Lookup Table."""
    return _live_result_tables(app_db) | _lookup_tables(app_db)


def _table_kind(app_db: Path | None, name: str) -> str | None:
    """The kind of a readable table (`lookup` or `result`), or None if there is no such readable table."""
    if name in _lookup_tables(app_db):
        return "lookup"
    return "result" if name in _live_result_tables(app_db) else None


CHART_GUIDANCE = (
    'The spec\'s data must be exactly {"name": "<result table>"} naming an existing Result Table; '
    "inline values, URLs, datasets and other sources are not allowed."
)


def _find_violation(node, table: str) -> str | None:
    """Walk a Vega-Lite spec; every `data` must be {"name": table}; no datasets/urls."""
    if isinstance(node, dict):
        for key, value in node.items():
            if key == "datasets" or key == "url":
                return f"'{key}' is not allowed. " + CHART_GUIDANCE
            if key == "data" and value != {"name": table}:
                return "Invalid data source. " + CHART_GUIDANCE
            found = _find_violation(value, table)
            if found:
                return found
    elif isinstance(node, list):
        for item in node:
            found = _find_violation(item, table)
            if found:
                return found
    return None


def create_server(settings: ServerSettings) -> FastMCP:
    """The MCP server with its tools bound to these static settings."""
    mcp = FastMCP("flood-agent")

    @mcp.tool()
    @_in_worker_thread
    def run_query(sql: str, title: str, ctx: Context) -> str:
        """Run a single read-only SELECT over the Telemetry Database. The output is stored in
        a new Result Table (named by the tool). Returns only Query Metadata, never rows."""
        # The SELECT may reference any live Result Table (ADR 0003).
        allowed = _readable_tables(settings.app_db)
        # A failed result may carry `raw_error`; the Agent strips it (keeping it for the Trace)
        # before anything is sent to the LLM.
        return json.dumps(
            queries.run_query(
                settings.telemetry_db, settings.results_db, sql, settings.query_timeout_s,
                _max_result_rows(ctx), allowed, title,
            )
        )

    @mcp.tool()
    @_in_worker_thread
    def run_count_query(sql: str, ctx: Context) -> str:
        """Run a single read-only SELECT that returns one number, e.g. SELECT COUNT(*) FROM ...
        No Result Table is created; the count itself is returned to you and you may state it."""
        allowed = _readable_tables(settings.app_db)
        return json.dumps(
            queries.run_count_query(settings.telemetry_db, settings.results_db, sql, settings.query_timeout_s, allowed)
        )

    @mcp.tool()
    @_in_worker_thread
    def describe_table(name: str) -> str:
        """Describe a table the user names: its kind, column names and types, and row count. Works
        for any live table from any chat, not only those listed in your prompt. Never returns
        rows. Query it by name with run_query."""
        kind = _table_kind(settings.app_db, name)
        described = queries.describe_results_table(settings.results_db, name) if kind else None
        if described is None:
            return json.dumps({"success": False, "error": "No such table."})
        return json.dumps({"success": True, "name": name, "kind": kind, **described})

    @mcp.tool()
    @_in_worker_thread
    def show_table(result_table: str, ctx: Context, columns: list[str] | None = None) -> str:
        """Show a Result Table to the user as a table Display. You will not see the rows."""
        # Another Conversation's table is reported exactly like one that does not exist.
        owned = result_table in _allowed_tables(settings.app_db, ctx)
        available = queries.result_table_columns(settings.results_db, result_table) if owned else None
        if available is None:
            return json.dumps({"success": False, "error": "Unknown Result Table."})
        if columns is not None and not set(columns) <= set(available):
            return json.dumps({"success": False, "error": "Unknown column requested."})
        display = {"kind": "table", "result_table": result_table, "columns": columns or available}
        return json.dumps({"success": True, "display": display})

    @mcp.tool()
    @_in_worker_thread
    def show_chart(vega_lite_spec: dict, ctx: Context) -> str:
        """Show a Vega-Lite Chart Spec to the user as a chart Display. Its data must be
        {"name": "<result table>"}; never include data values. Aggregate first if the Result
        Table is large: a chart shows at most its first CHART_ROW_CAP rows. Use temporal x scales of type "utc" for epoch-ms time columns."""
        data = vega_lite_spec.get("data")
        table = data.get("name") if isinstance(data, dict) else None
        if not isinstance(table, str):
            return json.dumps({"success": False, "error": "Missing data source. " + CHART_GUIDANCE})
        owned = table in _allowed_tables(settings.app_db, ctx)
        count = queries.result_table_row_count(settings.results_db, table) if owned else None
        if count is None:
            return json.dumps({"success": False, "error": "Unknown Result Table. " + CHART_GUIDANCE})
        violation = _find_violation(vega_lite_spec, table)
        if violation:
            return json.dumps({"success": False, "error": violation})
        result: dict = {"success": True, "display": {"kind": "chart", "result_table": table, "spec": vega_lite_spec}}
        if count > settings.chart_row_cap:
            result["note"] = (
                f"Only the first {settings.chart_row_cap} rows of the Result Table are charted. Do not present the "
                "chart as covering all the data; to chart everything, aggregate or filter with run_query "
                "(e.g. GROUP BY a time bucket) and chart the new Result Table."
            )
        return json.dumps(result)

    return mcp


if __name__ == "__main__":
    create_server(ServerSettings.from_env(os.environ)).run(transport="stdio")
