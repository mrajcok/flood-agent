"""MCP server. Exposes exactly the LLM-facing tools.

No tool here returns rows: `run_query` returns Query Metadata, `run_count_query` returns a
single Count (the ADR 0001 exception), `show_table` records a Display definition. Rows reach the
browser only through the Agent's internal data path.

`create_server(settings)` builds the server from static `ServerSettings`. In production it runs
as a stdio subprocess (`python -m flood_agent.mcp_server`) and reads those settings from the
launch environment the Agent passes it: TELEMETRY_DB, RESULTS_DB, APP_DB, QUERY_TIMEOUT_S,
and CHART_ROW_CAP. There are no fallbacks. Most tests run it in-process
(`tests/conftest.py`).

Per-turn context arrives with every tool call in the MCP request `_meta`, set by the Agent and
never a tool argument, so the LLM never sees it. The server reads only the turn's row cap
(`flood_agent/max_result_rows`). Tables are not scoped to a Conversation (ADR 0003): every call may
read and show any live Result Table, and read any Lookup Table.
"""

import functools
import json
import os
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import anyio.to_thread
from mcp.server.fastmcp import Context, FastMCP

from flood_agent import logs, queries
from flood_agent.catalog import CatalogView
from flood_agent.mcp_protocol import META_MAX_RESULT_ROWS


@dataclass(frozen=True)
class ServerSettings:
    """The server's static launch settings. `app_db` None means no Result Tables are owned."""

    telemetry_db: Path
    results_db: Path
    app_db: Path | None
    query_timeout_s: float
    chart_row_cap: int
    telemetry_tables: tuple[str, ...] = ()  # Telemetry Database table names, static, from the Semantic Layer

    def to_env(self) -> dict[str, str]:
        env = {
            "TELEMETRY_DB": str(self.telemetry_db),
            "RESULTS_DB": str(self.results_db),
            "QUERY_TIMEOUT_S": str(self.query_timeout_s),
            "CHART_ROW_CAP": str(self.chart_row_cap),
            "TELEMETRY_TABLES": json.dumps(list(self.telemetry_tables)),
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
            telemetry_tables=tuple(json.loads(env.get("TELEMETRY_TABLES", "[]"))),
        )


def _wall_clock() -> int:
    return int(time.time() * 1000)


def _in_worker_thread(fn):
    """Run a sync tool body in a worker thread. FastMCP calls sync tools directly on its event
    loop, so one slow `run_query` would stall every other Conversation's calls (ADR 0002)."""

    @functools.wraps(fn)  # keeps the signature, so FastMCP builds the same input schema
    async def wrapper(*args, **kwargs):
        return await anyio.to_thread.run_sync(functools.partial(fn, *args, **kwargs))

    return wrapper


def _max_result_rows(ctx: Context) -> int:
    """This turn's row cap from request `_meta`. There is no default: a call without a valid cap fails."""
    meta = ctx.request_context.meta
    value = getattr(meta, META_MAX_RESULT_ROWS, None) if meta else None
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise ValueError("The request must carry a max_result_rows of at least 1 in its _meta.")
    return value


LOOKUP_NOT_SHOWABLE = (
    "That is a Lookup Table, which cannot be shown directly. Query it with run_query to make a "
    "Result Table, then show that."
)


def _rename_data(node: Any, old: str, new: str) -> Any:
    """A Chart Spec with every {"name": old} data reference pointing at `new`."""
    if isinstance(node, dict):
        return {"name": new} if node == {"name": old} else {k: _rename_data(v, old, new) for k, v in node.items()}
    if isinstance(node, list):
        return [_rename_data(i, old, new) for i in node]
    return node


def _not_showable(catalog: CatalogView | None, name: str) -> str:
    """The error for a table show_table/show_chart cannot show. A Lookup Table gets guidance to
    query it; an expired table is reported exactly like one that does not exist."""
    if catalog is not None and catalog.kind_of(name) == "lookup":
        return LOOKUP_NOT_SHOWABLE
    return "Unknown Result Table."


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


MARKS = (
    "arc", "area", "bar", "boxplot", "circle", "errorband", "errorbar", "geoshape", "image", "line",
    "point", "rect", "rule", "square", "text", "tick", "trail",
)
FIELD_TYPES = ("quantitative", "temporal", "nominal", "ordinal")
_COMPOSITE = ("layer", "concat", "hconcat", "vconcat", "facet", "repeat", "spec")


def _transform_outputs(node) -> set[str]:
    """Field names the spec's transforms create ("as": "x" or ["x", "y"]), anywhere in it."""
    if isinstance(node, list):
        return set().union(*(_transform_outputs(item) for item in node))
    if not isinstance(node, dict):
        return set()
    out: set[str] = set()
    for key, value in node.items():
        if key == "as":
            out |= {value} if isinstance(value, str) else {v for v in value if isinstance(v, str)} if isinstance(value, list) else set()
        else:
            out |= _transform_outputs(value)
    return out


def _unknown_field(field: str) -> str:
    return f'Unknown field "{field}". Field names are case-sensitive.'


def _encoding_problem(node, fields: set[str]) -> str | None:
    """The first field definition under an encoding that names an unknown field or lacks a valid type."""
    if isinstance(node, list):
        return next((p for item in node if (p := _encoding_problem(item, fields))), None)
    if not isinstance(node, dict):
        return None
    field = node.get("field")
    if isinstance(field, str):  # a {"repeat": ...} field is not a string
        if field not in fields:
            return _unknown_field(field)
        if node.get("type") not in FIELD_TYPES:
            got = f'has type {json.dumps(node["type"])}; use' if "type" in node else 'needs a "type":'
            return f'Field "{field}" {got} one of {", ".join(FIELD_TYPES)}.'
    for key, value in node.items():
        if key == "sort" and isinstance(value, dict):  # a sort definition: a field, never a type
            sort_field = value.get("field")
            if isinstance(sort_field, str) and sort_field not in fields:
                return _unknown_field(sort_field)
        elif found := _encoding_problem(value, fields):
            return found
    return None


def _spec_problem(node, fields: set[str], top: bool = True) -> str | None:
    """What would make the spec draw nothing, found without rendering it: an unknown or missing
    mark, or an encoding field that is not a column (or a transform's output) or has no valid type."""
    if isinstance(node, list):
        return next((p for item in node if (p := _spec_problem(item, fields, top=False))), None)
    if not isinstance(node, dict):
        return None
    if "mark" in node:
        mark = node["mark"]
        name = mark.get("type") if isinstance(mark, dict) else mark
        if name not in MARKS:
            return f"Unknown mark {json.dumps(name)}. Use one of: " + ", ".join(f'"{m}"' for m in MARKS) + "."
    elif top and not any(k in node for k in _COMPOSITE):
        return 'The spec needs a "mark", for example "bar", "line" or "rect".'
    for key, value in node.items():
        if key == "encoding":
            found = _encoding_problem(value, fields)
        elif key in _COMPOSITE:
            found = _spec_problem(value, fields, top=False)
        else:
            continue
        if found:
            return found
    return None


def create_server(settings: ServerSettings, clock: Callable[[], int] | None = None) -> FastMCP:
    """The MCP server with its tools bound to these static settings. Tables are read through a read-only
    Catalog view, whose liveness is worked out on every call from `clock` (epoch ms; the wall clock by default)."""
    mcp = FastMCP("flood-agent")
    catalog = (
        CatalogView(settings.app_db, clock or _wall_clock, settings.telemetry_tables) if settings.app_db else None
    )

    @mcp.tool()
    @_in_worker_thread
    def run_query(sql: str, title: str, ctx: Context) -> str:
        """Run a single read-only SELECT over the Telemetry Database. The output is stored in
        a new Result Table (named by the tool). Returns only Query Metadata, never rows."""
        # The SELECT may reference any live Result Table (ADR 0003).
        allowed = catalog.readable() if catalog else set()
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
        allowed = catalog.readable() if catalog else set()
        return json.dumps(
            queries.run_count_query(settings.telemetry_db, settings.results_db, sql, settings.query_timeout_s, allowed)
        )

    @mcp.tool()
    @_in_worker_thread
    def describe_table(name: str) -> str:
        """Describe a table the user names: its kind, column names and types, and row count. Works
        for any live table from any chat, not only those listed in your prompt. Never returns
        rows. Query it by name with run_query."""
        name = name.lower()  # table names are kept in lower case (ADR 0005)
        kind = catalog.kind_of(name) if catalog else None
        kind = kind if kind in ("lookup", "result", "saved") else None
        described = queries.describe_results_table(settings.results_db, name) if kind else None
        if described is None:
            return json.dumps({"success": False, "error": "No such table."})
        return json.dumps({"success": True, "name": name, "kind": kind, **described})

    @mcp.tool()
    @_in_worker_thread
    def show_table(result_table: str, columns: list[str] | None = None) -> str:
        """Show a live Result Table, from any chat, to the user as a table Display. You
        will not see the rows."""
        # Any live Result Table may be shown (ADR 0003).
        result_table = result_table.lower()  # table names are kept in lower case (ADR 0005)
        live = catalog is not None and result_table in catalog
        available = queries.result_table_columns(settings.results_db, result_table) if live else None
        if available is None:
            return json.dumps({"success": False, "error": _not_showable(catalog, result_table)})
        if columns is not None and not set(columns) <= set(available):
            return json.dumps({"success": False, "error": "Unknown column requested."})
        display = {"kind": "table", "result_table": result_table, "columns": columns or available}
        return json.dumps({"success": True, "display": display})

    @mcp.tool()
    @_in_worker_thread
    def show_chart(vega_lite_spec: dict) -> str:
        """Show a Vega-Lite Chart Spec to the user as a chart Display. Its data must be
        {"name": "<result table>"}; never include data values. Aggregate first if the Result
        Table is large: a chart shows at most its first CHART_ROW_CAP rows. Every encoded "field" must be a
        column name, exactly as in Query Metadata, with a "type" (quantitative, temporal, nominal, ordinal).
        Use temporal x scales of type "utc" for epoch-ms time columns. Don't set "width"."""
        data = vega_lite_spec.get("data")
        table = data.get("name") if isinstance(data, dict) else None
        if not isinstance(table, str):
            return json.dumps({"success": False, "error": "Missing data source. " + CHART_GUIDANCE})
        if table != table.lower():  # names are kept in lower case (ADR 0005): chart the table by that name
            vega_lite_spec = _rename_data(vega_lite_spec, table, table.lower())
            table = table.lower()
        live = catalog is not None and table in catalog
        count = queries.result_table_row_count(settings.results_db, table) if live else None
        if count is None:
            return json.dumps({"success": False, "error": f"{_not_showable(catalog, table)} {CHART_GUIDANCE}"})
        violation = _find_violation(vega_lite_spec, table)
        if violation:
            return json.dumps({"success": False, "error": violation})
        columns = queries.result_table_columns(settings.results_db, table) or []
        problem = _spec_problem(vega_lite_spec, set(columns) | _transform_outputs(vega_lite_spec))
        if problem:
            return json.dumps({"success": False, "error": f"{problem} The columns of {table} are: {', '.join(columns)}."})
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
    logs.configure_root()  # before FastMCP, which would otherwise log bare messages with no level
    create_server(ServerSettings.from_env(os.environ)).run(transport="stdio")
