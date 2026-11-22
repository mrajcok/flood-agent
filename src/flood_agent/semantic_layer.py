"""The Semantic Layer: a hand-authored YAML file rendered into the system prompt."""

from pathlib import Path
from typing import TYPE_CHECKING

import yaml

from flood_agent.ai_cell_context import SystemPrompt

if TYPE_CHECKING:  # annotations only: no import-time coupling to the Catalog
    from flood_agent.catalog import Table


SYSTEM_INSTRUCTIONS = """\
You help analysts explore network telemetry (flow records and DNS response metadata) by writing SQL. You never see any
data: only the Semantic Layer below and Query Metadata (column names, types, row
counts). Rules:
- Use run_query to create a Result Table from a single SELECT. It returns only metadata.
- To get just a count (how many rows match?) call run_count_query with a SELECT COUNT(...)
  query. It returns the number to you, with no Result Table, and you may state it, written
  with thousands separators (e.g. 17,392). Use
  run_query when the user wants to see rows or a chart.
- To show data to the user call show_table with the Result Table name. Rows go
  straight to the user's screen; you cannot read them.
- To chart data call show_chart with a Vega-Lite spec whose data is exactly
  {"name": "<Result Table name>"}; never include data values, urls or datasets. Shape the
  Result Table with run_query first so the chart needs no transforms, and aggregate to
  fewer rows than the chart limit (e.g. GROUP BY a time bucket, or top N with LIMIT).
  Every encoding needs "field" (a Result Table column name, spelled and cased exactly as in
  Query Metadata) and "type" (quantitative, temporal, nominal or ordinal). Numeric columns
  (INTEGER, REAL) are quantitative; IPs, ports and protocols used as categories are nominal.
  Don't set "width": the page sizes the chart to fit. Chart types, as complete examples:
  - time series (time columns are epoch ms; always UTC, never local time; add a "color"
    nominal field for several series):
    {"data": {"name": "<rt>"}, "mark": "line", "encoding": {
     "x": {"field": "bucket_ms", "type": "temporal", "scale": {"type": "utc"}, "title": "Time (UTC)"},
     "y": {"field": "total_bytes", "type": "quantitative", "title": "Bytes"}}}
  - top-N (horizontal bars; "height": {"step": 16} keeps many bars readable):
    {"data": {"name": "<rt>"}, "mark": "bar", "height": {"step": 16}, "encoding": {
     "y": {"field": "dip", "type": "nominal", "sort": "-x", "title": "Destination IP"},
     "x": {"field": "total_bytes", "type": "quantitative", "title": "Bytes"}}}
  - heatmap: mark "rect", two discrete axes (e.g. time bucket by dport, both "ordinal" or
    "nominal"), and a quantitative "color" field.
  Give each axis a readable "title". Charts do not use Semantic Layer formatting, so
  convert units in SQL when helpful (e.g. bytes / 1e6 AS mbytes) and say so in the title.
  If show_chart fails, fix the spec or aggregate further and retry.
  You cannot see the rendered chart. If the user says a chart is blank or wrong, do not
  guess a cause: check each field and type against describe_table, fix what is actually
  wrong, or ask the user what they see.
- Your reply text and the tables and charts appear in the order you produce them. Introduce a
  table or chart before you call show_table or show_chart. After it, add only caveats or
  next steps, never another "here is".
- Never state data values (numbers, IPs, circuits) you have not been told by the user
  or received from run_count_query.
  You cannot know them: do not guess, estimate or recall them. Point the user to the
  table or chart instead. Any IP address or large number in your reply that is not in the
  user's messages, the Semantic Layer or Query Metadata, or a run_count_query count is flagged to the user as unverified.
- Some user messages are labelled. "[Markdown note] ..." is a note the user wrote in the notebook.
  Other labelled messages report queries the user ran themselves, without you. Treat all of them
  as the user's own words and work. You may state a row count, a count query's number and the
  rows of a SHOW or DESCRIBE result that such a message gives you. You were never sent any other
  values from those queries, so do not state any.
- Filter time ranges on the Time Key column (etime), never on stime.
- Resolve relative time phrases ("last hour", "yesterday", "past 15 minutes") with the SQL
  function now() (epoch ms), e.g. `etime >= now() - 2 * 3600 * 1000`. You are not told the
  current time and cannot know it: never write an absolute timestamp for a relative phrase.
  When you mention a time to
  the user, write it as a UTC date and time (e.g. 2026-01-07 23:00 UTC), not epoch ms.
- Map names (protocols, flags, record types, response codes) to values with the Semantic Layer
  enums; a column with `enum_bits` is a bitmask, so test it with `col & bit != 0`.
- Use the functions and metrics documented in the Semantic Layer (e.g. in_cidr for CIDR filters).
"""


class SemanticLayer:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.data = yaml.safe_load(self.path.read_text())
        self._validate()

    def time_key(self, table: str) -> str:
        """The table's own `time_key`, else the global default ("" if neither is set)."""
        own = ((self.data.get("tables") or {}).get(table) or {}).get("time_key")
        return own or self.data.get("time_key") or ""

    def _validate(self) -> None:
        """Refuse to start on a table without its Time Key column, or on one column name with two formats."""
        tables = self.data.get("tables") or {}
        seen: dict[str, tuple[str, tuple]] = {}
        for table, spec in tables.items():
            cols = (spec or {}).get("columns") or {}
            key = self.time_key(table)
            if key and key not in cols:
                raise ValueError(f"Semantic Layer: table {table!r} has no Time Key column {key!r}")
            for name, meta in cols.items():
                meta = meta or {}
                fmt = (meta.get("unit"), meta.get("enum"), meta.get("enum_bits"))
                if name in seen and seen[name][1] != fmt:
                    raise ValueError(
                        f"Semantic Layer: column {name!r} has different unit/enum/enum_bits in "
                        f"tables {seen[name][0]!r} and {table!r}"
                    )
                seen.setdefault(name, (table, fmt))

    def table_names(self) -> list[str]:
        """Names of the Telemetry Database tables described in the Semantic Layer's `tables:` map."""
        return list(self.data.get("tables") or {})

    def column_formats(self, columns: list[str]) -> dict:
        """Display format per Result Table column, from Semantic Layer column metadata.

        A Result Table column is matched to a source column by identical name in any Telemetry table; aliased
        or derived columns (e.g. `bytes_total`) get plain formatting."""
        meta: dict = {}
        for spec in (self.data.get("tables") or {}).values():
            for name, m in ((spec or {}).get("columns") or {}).items():
                meta.setdefault(name, m)
        out = {}
        for c in columns:
            m = meta.get(c) or {}
            unit = str(m.get("unit", "")).lower()
            if m.get("enum_bits"):
                out[c] = {"kind": "flags", "map": {str(k): str(v) for k, v in m["enum_bits"].items()}}
            elif m.get("enum"):
                out[c] = {"kind": "enum", "map": {str(k): str(v) for k, v in m["enum"].items()}}
            elif unit == "bytes":
                out[c] = {"kind": "bytes"}
            elif unit.startswith("epoch ms"):
                out[c] = {"kind": "time"}
            else:
                out[c] = {"kind": "plain"}
        return out

    def render(self) -> str:
        return yaml.safe_dump(self.data, sort_keys=False)

    def system_prompt(self, result_tables: "list[Table] | None" = None) -> str:
        """The system prompt, assembled by the AI Cell context module from these instructions, this layer and the tables."""
        return SystemPrompt.build(SYSTEM_INSTRUCTIONS, self.render(), result_tables or []).render()
