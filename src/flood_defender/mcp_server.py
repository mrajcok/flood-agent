"""MCP server (stdio subprocess). Exposes exactly the LLM-facing tools.

No tool here returns rows: `run_query` returns Query Metadata, `show_table` records a
Display definition. Rows reach the browser only through the Agent's internal data path.

Static configuration comes from the environment the Agent passes it: TELEMETRY_DB, RESULTS_DB,
APP_DB, QUERY_TIMEOUT_S, MAX_RESULT_ROWS and CHART_ROW_CAP. There are no fallbacks.

The Conversation id that limits which Result Tables a call may use arrives with every tool call in
the MCP request `_meta` (key `flood_defender/conversation_id`), set by the Agent. It is not a tool
argument, so the LLM never sees it. A missing or empty id means no Result Tables are owned.
"""

import functools
import json
import os
import sqlite3
from pathlib import Path

import anyio.to_thread
from mcp.server.fastmcp import Context, FastMCP

from flood_defender import queries
from flood_defender.mcp_protocol import META_CONVERSATION_ID

mcp = FastMCP("flood-defender")


def _in_worker_thread(fn):
    """Run a sync tool body in a worker thread. FastMCP calls sync tools directly on its event
    loop, so one slow `run_query` would stall every other Conversation's calls (ADR 0002)."""

    @functools.wraps(fn)  # keeps the signature, so FastMCP builds the same input schema
    async def wrapper(*args, **kwargs):
        return await anyio.to_thread.run_sync(functools.partial(fn, *args, **kwargs))

    return wrapper


def _paths() -> tuple[Path, Path]:
    return Path(os.environ["TELEMETRY_DB"]), Path(os.environ["RESULTS_DB"])


def _conversation_id(ctx: Context) -> str | None:
    meta = ctx.request_context.meta
    value = getattr(meta, META_CONVERSATION_ID, None) if meta else None
    return value if isinstance(value, str) and value else None


def _allowed_tables(ctx: Context) -> set[str]:
    """The calling Conversation's live Result Tables, read live from the app database with a
    read-only query (no DDL, no write lock). Empty if the Conversation id or APP_DB is missing."""
    app_db, conversation_id = os.environ.get("APP_DB"), _conversation_id(ctx)
    if not app_db or not conversation_id:
        return set()
    con = sqlite3.connect(f"file:{app_db}?mode=ro", uri=True, timeout=5)
    try:
        rows = con.execute(
            "SELECT name FROM result_tables WHERE conversation_id=? AND expired=0", (conversation_id,)
        )
        return {r[0] for r in rows}
    finally:
        con.close()


@mcp.tool()
@_in_worker_thread
def run_query(sql: str, title: str, ctx: Context) -> str:
    """Run a single read-only SELECT over the Telemetry Database. The output is stored in
    a new Result Table (named by the tool). Returns only Query Metadata, never rows."""
    telemetry, results = _paths()
    timeout = float(os.environ["QUERY_TIMEOUT_S"])
    max_rows = int(os.environ["MAX_RESULT_ROWS"])
    # The SELECT may reference only this Conversation's live Result Tables.
    allowed = _allowed_tables(ctx)
    # A failed result may carry `raw_error`; the Agent strips it (keeping it for the Trace)
    # before anything is sent to the LLM.
    return json.dumps(queries.run_query(telemetry, results, sql, timeout, max_rows, allowed))


@mcp.tool()
@_in_worker_thread
def show_table(result_table: str, ctx: Context, columns: list[str] | None = None) -> str:
    """Show a Result Table to the user as a table Display. You will not see the rows."""
    _, results = _paths()
    # Another Conversation's table is reported exactly like one that does not exist.
    available = (
        queries.result_table_columns(results, result_table) if result_table in _allowed_tables(ctx) else None
    )
    if available is None:
        return json.dumps({"success": False, "error": "Unknown Result Table."})
    if columns is not None and not set(columns) <= set(available):
        return json.dumps({"success": False, "error": "Unknown column requested."})
    display = {"kind": "table", "result_table": result_table, "columns": columns or available}
    return json.dumps({"success": True, "display": display})


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


@mcp.tool()
@_in_worker_thread
def show_chart(vega_lite_spec: dict, ctx: Context) -> str:
    """Show a Vega-Lite Chart Spec to the user. Its data must be {"name": "<result table>"};
    never include data values. Aggregate first if the Result Table is large."""
    _, results = _paths()
    data = vega_lite_spec.get("data")
    table = data.get("name") if isinstance(data, dict) else None
    if not isinstance(table, str):
        return json.dumps({"success": False, "error": "Missing data source. " + CHART_GUIDANCE})
    count = queries.result_table_row_count(results, table) if table in _allowed_tables(ctx) else None
    if count is None:
        return json.dumps({"success": False, "error": "Unknown Result Table. " + CHART_GUIDANCE})
    violation = _find_violation(vega_lite_spec, table)
    if violation:
        return json.dumps({"success": False, "error": violation})
    cap = int(os.environ["CHART_ROW_CAP"])
    if count > cap:
        return json.dumps(
            {
                "success": False,
                "error": f"Result Table has too many rows to chart (limit {cap}). "
                "Aggregate or filter with run_query (e.g. GROUP BY a time bucket or LIMIT to top N) "
                "and chart the new Result Table.",
            }
        )
    return json.dumps({"success": True, "display": {"kind": "chart", "result_table": table, "spec": vega_lite_spec}})


if __name__ == "__main__":
    mcp.run(transport="stdio")
