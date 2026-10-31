"""MCP server (stdio subprocess). Exposes exactly the LLM-facing tools.

No tool here returns rows: `run_query` returns Query Metadata, `show_table` records a
Display definition. Rows reach the browser only through the Agent's internal data path.

Configuration comes from the environment: TELEMETRY_DB and RESULTS_DB.
"""

import json
import os
from pathlib import Path

from mcp.server.fastmcp import FastMCP

from flood_defender import queries
from flood_defender.store import ConversationStore

mcp = FastMCP("flood-defender")


def _paths() -> tuple[Path, Path]:
    return Path(os.environ["TELEMETRY_DB"]), Path(os.environ["RESULTS_DB"])


@mcp.tool()
def run_query(sql: str, title: str) -> str:
    """Run a single read-only SELECT over the Telemetry Database. The output is stored in
    a new Result Table (named by the tool). Returns only Query Metadata, never rows."""
    telemetry, results = _paths()
    # The SELECT may reference only this Conversation's live Result Tables.
    allowed = set()
    if os.environ.get("APP_DB") and os.environ.get("CONVERSATION_ID"):
        store = ConversationStore(Path(os.environ["APP_DB"]))
        allowed = set(store.result_tables(os.environ["CONVERSATION_ID"]))
    return json.dumps(queries.run_query(telemetry, results, sql, allowed))


@mcp.tool()
def show_table(result_table: str, columns: list[str] | None = None) -> str:
    """Show a Result Table to the user as a table Display. You will not see the rows."""
    _, results = _paths()
    available = queries.result_table_columns(results, result_table)
    if available is None:
        return json.dumps({"success": False, "error": "Unknown Result Table."})
    if columns is not None and not set(columns) <= set(available):
        return json.dumps({"success": False, "error": "Unknown column requested."})
    display = {"kind": "table", "result_table": result_table, "columns": columns or available}
    return json.dumps({"success": True, "display": display})


if __name__ == "__main__":
    mcp.run(transport="stdio")
