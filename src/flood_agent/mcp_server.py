"""MCP server. Exposes exactly the LLM-facing tools.

No tool here returns rows: `run_query` returns Query Metadata, `run_count_query` returns a
single Count (the ADR 0001 exception), `show_table` records a Display definition. Rows reach the
browser only through the Agent's internal data path.

`create_server(settings)` builds the server from static `ServerSettings`. In production it runs
as a stdio subprocess (`python -m flood_agent.mcp_server`) and reads those settings from the
launch environment the Agent passes it: TELEMETRY_DB, USERS_DIR, USER_NAMES, QUERY_TIMEOUT_S,
and CHART_ROW_CAP. There are no fallbacks. Most tests run it in-process
(`tests/conftest.py`).

Per-call context arrives with every tool call in the MCP request `_meta`, set by the Agent and
never a tool argument, so the LLM never sees it: the User the call is for (`flood_agent/user`) and
the AI Cell's row cap (`flood_agent/max_result_rows`). A call opens that User's results database and
Table Catalog and no other User's. Tables are not scoped to a Notebook (ADR 0003): every call may
read and show any of the User's live Result Tables, and read any of their Lookup Tables.
"""

import functools
import json
import os
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path

import anyio.to_thread
from mcp.server.fastmcp import Context, FastMCP

from flood_agent import logs, queries
from flood_agent.catalog import CatalogView
from flood_agent.mcp_protocol import META_MAX_RESULT_ROWS, META_USER
from flood_agent.notebook_store import rename_data_source
from flood_agent.tool_outcome import DisplayMade, Failed, TableDescribed, encode, from_query_result
from flood_agent.users import user_databases


@dataclass(frozen=True)
class ServerSettings:
    """The server's static launch settings. `users` are the User names a call may be made for, each with their
    databases under `users_dir`."""

    telemetry_db: Path
    users_dir: Path
    users: tuple[str, ...]
    query_timeout_s: float
    chart_row_cap: int
    # Each Telemetry Database table mapped to its Restricted Columns, static, from the Semantic Layer
    restricted_columns_by_table: dict[str, tuple[str, ...]] = field(default_factory=dict)
    fixed_now_ms: int | None = None  # pins SQL now() (evals); None is the wall clock

    def to_env(self) -> dict[str, str]:
        env = {
            "TELEMETRY_DB": str(self.telemetry_db),
            "USERS_DIR": str(self.users_dir),
            "USER_NAMES": json.dumps(list(self.users)),
            "QUERY_TIMEOUT_S": str(self.query_timeout_s),
            "CHART_ROW_CAP": str(self.chart_row_cap),
            "RESTRICTED_COLUMNS_BY_TABLE": json.dumps(self.restricted_columns_by_table),
        }
        if self.fixed_now_ms is not None:
            env["FIXED_NOW_MS"] = str(self.fixed_now_ms)
        return env

    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> "ServerSettings":
        return cls(
            telemetry_db=Path(env["TELEMETRY_DB"]),
            users_dir=Path(env["USERS_DIR"]),
            users=tuple(json.loads(env["USER_NAMES"])),
            query_timeout_s=float(env["QUERY_TIMEOUT_S"]),
            chart_row_cap=int(env["CHART_ROW_CAP"]),
            restricted_columns_by_table={
                name: tuple(columns)
                for name, columns in json.loads(env.get("RESTRICTED_COLUMNS_BY_TABLE", "{}")).items()},
            fixed_now_ms=int(env["FIXED_NOW_MS"]) if env.get("FIXED_NOW_MS") else None,
        )


def _wall_clock() -> int:
    return int(time.time() * 1000)


def _in_worker_thread(fn):
    """Run a sync tool body in a worker thread. FastMCP calls sync tools directly on its event
    loop, so one slow `run_query` would stall every other call (ADR 0002)."""

    @functools.wraps(fn)  # keeps the signature, so FastMCP builds the same input schema
    async def wrapper(*args, **kwargs):
        return await anyio.to_thread.run_sync(functools.partial(fn, *args, **kwargs))

    return wrapper


def _meta(ctx: Context, key: str):
    """The value of `key` in the request's `_meta`, or None."""
    meta = ctx.request_context.meta
    return getattr(meta, key, None) if meta else None


def _max_result_rows(ctx: Context) -> int:
    """This turn's row cap from request `_meta`. There is no default: a call without a valid cap fails."""
    value = _meta(ctx, META_MAX_RESULT_ROWS)
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise ValueError("The request must carry a max_result_rows of at least 1 in its _meta.")
    return value


LOOKUP_NOT_SHOWABLE = (
    "That is a Lookup Table, which cannot be shown directly. Query it with run_query to make a "
    "Result Table, then show that."
)


def _not_showable(catalog: CatalogView, name: str) -> str:
    """The error for a table show_table/show_chart cannot show. A Lookup Table gets guidance to
    query it; an expired table is reported exactly like one that does not exist."""
    if catalog.kind_of(name) == "lookup":
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
    """The MCP server with its tools bound to these static settings. Each call reads the calling User's tables through a
    read-only Catalog view, whose liveness is worked out from `clock` (epoch ms; the wall clock by default), and
    queries through the server's own query runner over that User's results database (ADR 0002), which lets a SELECT
    read any of their live Result Tables, Saved Tables or Lookup Tables (ADR 0003)."""
    mcp = FastMCP("flood-agent")

    def for_caller(ctx: Context) -> tuple[CatalogView, queries.QueryRunner]:
        """The Catalog view and query runner of the User named in request `_meta`. There is no default: a call without
        a known User fails."""
        user = _meta(ctx, META_USER)
        if not isinstance(user, str) or user not in settings.users:
            raise ValueError("The request must carry a known User in its _meta.")
        databases = user_databases(settings.users_dir, user)
        catalog = CatalogView(databases.app_db, clock or _wall_clock, settings.restricted_columns_by_table)
        runner = queries.QueryRunner(
            settings.telemetry_db, databases.results_db, settings.query_timeout_s, catalog.readable,
            catalog.recorded_sources, catalog.holds)
        return catalog, runner

    @mcp.tool()
    @_in_worker_thread
    def run_query(sql: str, title: str, ctx: Context) -> str:
        """Run a single read-only SELECT over the Telemetry Database. The output is stored in
        a new Result Table (named by the tool). Returns only Query Metadata, never rows."""
        # The raw error and the executed CTAS travel in the outcome's internal part, for the Trace only.
        _, runner = for_caller(ctx)
        return encode(from_query_result(runner.run(sql, _max_result_rows(ctx), title)))

    @mcp.tool()
    @_in_worker_thread
    def run_count_query(sql: str, title: str, ctx: Context) -> str:
        """Run a single read-only SELECT that returns one number, e.g. SELECT COUNT(*) FROM ...
        No Result Table is created; the count itself is returned to you and you may state it. The user
        is shown it as a one-line Count Display labelled with `title` (a short name for what is counted)."""
        _, runner = for_caller(ctx)
        return encode(from_query_result(runner.count(sql)))

    @mcp.tool()
    @_in_worker_thread
    def describe_table(name: str, ctx: Context) -> str:
        """Describe a table the user names: its kind, column names and types, and row count. Works
        for any live table from any notebook, not only those listed in your prompt. Never returns
        rows. Query it by name with run_query."""
        catalog, runner = for_caller(ctx)
        kind = catalog.kind_of(name)
        kind = kind if kind in ("lookup", "result", "saved") else None
        name = CatalogView.canonical(name)
        described = runner.describe(name) if kind else None
        if described is None or kind is None:
            return encode(Failed("No such table."))
        return encode(TableDescribed(name, kind, described["columns"], described["row_count"]))

    @mcp.tool()
    @_in_worker_thread
    def show_table(result_table: str, ctx: Context, columns: list[str] | None = None) -> str:
        """Show a live Result Table, from any notebook, to the user as a table Display. You
        will not see the rows."""
        # Any live Result Table may be shown (ADR 0003).
        catalog, runner = for_caller(ctx)
        live = result_table in catalog
        result_table = CatalogView.canonical(result_table)
        available = runner.result_columns(result_table) if live else None
        if available is None:
            return encode(Failed(_not_showable(catalog, result_table)))
        if columns is not None and not set(columns) <= set(available):
            return encode(Failed("Unknown column requested."))
        display = {"kind": "table", "result_table": result_table, "columns": columns or available}
        return encode(DisplayMade(display))

    @mcp.tool()
    @_in_worker_thread
    def show_chart(vega_lite_spec: dict, ctx: Context) -> str:
        """Show a Vega-Lite Chart Spec to the user as a chart Display. Its data must be
        {"name": "<result table>"}; never include data values. Aggregate first if the Result
        Table is large: a chart shows at most its first CHART_ROW_CAP rows. Every encoded "field" must be a
        column name, exactly as in Query Metadata, with a "type" (quantitative, temporal, nominal, ordinal).
        Use temporal x scales of type "utc" for epoch-ms time columns. Don't set "width"."""
        catalog, runner = for_caller(ctx)
        data = vega_lite_spec.get("data")
        table = data.get("name") if isinstance(data, dict) else None
        if not isinstance(table, str):
            return encode(Failed("Missing data source. " + CHART_GUIDANCE))
        live = table in catalog
        if table != CatalogView.canonical(table):  # the Display records the table under its canonical name
            vega_lite_spec = rename_data_source(vega_lite_spec, table, CatalogView.canonical(table))
            table = CatalogView.canonical(table)
        count = runner.row_count(table) if live else None
        if count is None:
            return encode(Failed(f"{_not_showable(catalog, table)} {CHART_GUIDANCE}"))
        violation = _find_violation(vega_lite_spec, table)
        if violation:
            return encode(Failed(violation))
        columns = runner.result_columns(table) or []
        problem = _spec_problem(vega_lite_spec, set(columns) | _transform_outputs(vega_lite_spec))
        if problem:
            return encode(Failed(f"{problem} The columns of {table} are: {', '.join(columns)}."))
        note = None
        if count > settings.chart_row_cap:
            note = (
                f"Only the first {settings.chart_row_cap} rows of the Result Table are charted. Do not present the "
                "chart as covering all the data; to chart everything, aggregate or filter with run_query "
                "(e.g. GROUP BY a time bucket) and chart the new Result Table."
            )
        return encode(DisplayMade({"kind": "chart", "result_table": table, "spec": vega_lite_spec}, note))

    return mcp


if __name__ == "__main__":
    logs.configure_root()  # before FastMCP, which would otherwise log bare messages with no level
    create_server(ServerSettings.from_env(os.environ)).run(transport="stdio")
