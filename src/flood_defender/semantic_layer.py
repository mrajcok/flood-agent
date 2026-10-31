"""The Semantic Layer: a hand-authored YAML file rendered into the system prompt."""

from pathlib import Path

import yaml

SYSTEM_INSTRUCTIONS = """\
You help analysts explore network flow telemetry by writing SQL. You never see any
data: only the Semantic Layer below and Query Metadata (column names, types, row
counts). Rules:
- Use run_query to create a Result Table from a single SELECT. It returns only metadata.
- To show data to the user call show_table with the Result Table name. Rows go
  straight to the user's screen; you cannot read them.
- Never state data values (numbers, IPs, circuits) you have not been told by the user.
- Filter time ranges on the Time Key column (etime), never on stime.
- Resolve relative time phrases ("last hour", "yesterday", "past 15 minutes") against
  "now", which is the dataset's window_end, not the wall clock.
- Map protocol names and TCP flag names to values using the enums in the Semantic Layer
  (tcp_flags is a bitmask: test with `tcp_flags & 2 != 0`).
- Use the functions and metrics documented in the Semantic Layer (e.g. in_cidr for CIDR filters).
"""


class SemanticLayer:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.data = yaml.safe_load(self.path.read_text())

    @property
    def window_end(self):
        return self.data.get("window_end")

    def render(self) -> str:
        return yaml.safe_dump(self.data, sort_keys=False)

    def system_prompt(self, result_tables: list[dict] | None = None) -> str:
        return (
            f"{SYSTEM_INSTRUCTIONS}\n"
            f"The dataset ends at window_end; treat 'now' as epoch ms {self.window_end}.\n\n"
            f"# Semantic Layer\n{self.render()}"
            f"{self.render_result_tables(result_tables or [])}"
        )

    @staticmethod
    def render_result_tables(tables: list[dict]) -> str:
        """The Conversation's live Result Tables: names, schemas and originating requests only."""
        if not tables:
            return (
                "\n# Result Tables\nNone are available in this Conversation. Result Tables named in "
                "earlier messages have expired and cannot be queried.\n"
            )
        lines = [
            "\n# Result Tables",
            "Earlier results in this Conversation. Query them by name in run_query like any table "
            "(e.g. to break down or filter an earlier result). Others have expired.",
        ]
        for t in tables:
            cols = ", ".join(f"{c['name']} {c['type']}" for c in t["columns"])
            lines.append(f"- {t['name']} ({t['title']}): columns {cols}; from request: {t['request']!r}")
        return "\n".join(lines) + "\n"
