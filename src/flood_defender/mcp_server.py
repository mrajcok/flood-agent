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
def show_chart(vega_lite_spec: dict) -> str:
    """Show a Vega-Lite Chart Spec to the user. Its data must be {"name": "<result table>"};
    never include data values. Aggregate first if the Result Table is large."""
    _, results = _paths()
    data = vega_lite_spec.get("data")
    table = data.get("name") if isinstance(data, dict) else None
    if not isinstance(table, str):
        return json.dumps({"success": False, "error": "Missing data source. " + CHART_GUIDANCE})
    count = queries.result_table_row_count(results, table)
    if count is None:
        return json.dumps({"success": False, "error": "Unknown Result Table. " + CHART_GUIDANCE})
    violation = _find_violation(vega_lite_spec, table)
    if violation:
        return json.dumps({"success": False, "error": violation})
    cap = int(os.environ.get("CHART_ROW_CAP", "10000"))
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
