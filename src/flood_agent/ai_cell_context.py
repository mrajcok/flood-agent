"""The AI Cell context: what an AI Cell is sent. This part is the system prompt (ADR 0007).

The system prompt is a `SystemPrompt` of three named sections, always rendered in ADR 0007 order: fixed
instructions, then the Semantic Layer, then the Result Tables last, because they are the only part that changes
between steps. `parse_system_prompt` is the inverse, for the Trace, which stores the prompt as a plain string.
"""

from dataclasses import dataclass
from typing import TYPE_CHECKING

from flood_agent import table_names

if TYPE_CHECKING:  # annotations only: no import-time coupling to the Catalog
    from flood_agent.catalog import Table

SEMANTIC_LAYER_HEADING = "# Semantic Layer\n"
RESULT_TABLES_HEADING = "# Result Tables"


@dataclass(frozen=True)
class SystemPrompt:
    """The system prompt as sections, each holding its own text exactly as it appears in the rendered prompt.

    `instructions` ends with its blank line, `semantic_layer` starts with its heading, `result_tables` starts
    with its heading (empty when a stored prompt has none). Prompts without the headings land wholly in
    `instructions`."""

    instructions: str
    semantic_layer: str
    result_tables: str

    @classmethod
    def build(cls, instructions: str, semantic_layer: str, tables: "list[Table]") -> "SystemPrompt":
        """From the fixed instructions, the rendered Semantic Layer and the live Result Tables listed."""
        return cls(
            f"{instructions}\n\n",
            f"{SEMANTIC_LAYER_HEADING}{semantic_layer}",
            result_tables_section(tables),
        )

    def render(self) -> str:
        """Instructions, Semantic Layer, Result Tables last (ADR 0007): all but the last are stable, so the LLM caches them."""
        tail = f"\n{self.result_tables}" if self.result_tables else ""
        return f"{self.instructions}{self.semantic_layer}{tail}"

    def parts(self) -> tuple[str, str, str]:
        """(instructions, Semantic Layer, Result Tables), for the Trace to show separately."""
        return self.instructions, self.semantic_layer, self.result_tables


def parse_system_prompt(prompt: str | None) -> SystemPrompt:
    """A stored prompt string back into its sections; one without the expected headings lands wholly in the instructions."""
    prompt = prompt or ""
    head, sep, tail = prompt.partition(SEMANTIC_LAYER_HEADING)
    if not sep:
        return SystemPrompt(prompt, "", "")
    layer, rt_sep, rest = tail.partition(f"\n{RESULT_TABLES_HEADING}")
    return SystemPrompt(head, SEMANTIC_LAYER_HEADING + layer, (rt_sep + rest).lstrip("\n"))


def result_tables_section(tables: "list[Table]") -> str:
    """The live Result Tables of the cells above, and the cell's own earlier Runs: names, schemas and originating requests only."""
    if not tables:
        return (
            f"{RESULT_TABLES_HEADING}\nNone are available in the cells above. Result Tables named in "
            "earlier messages have expired and cannot be queried. If the user names another table, "
            "call describe_table(name) to see its columns, then query it by name; a live Result Table "
            "from another notebook can also be shown directly with show_table or show_chart.\n"
        )
    lines = [
        RESULT_TABLES_HEADING,
        "Earlier results in this notebook. Query them by name in run_query like any table "
        "(e.g. to break down or filter an earlier result). Others have expired. If the user names a "
        "table not listed here, call describe_table(name) to see its columns, then query it by name; a live "
        "Result Table from another notebook can also be shown directly with show_table or show_chart.",
    ]
    for t in tables:
        cols = ", ".join(f"{c['name']} {c['type']}" for c in t.columns)
        if t.request == table_names.SQL_CELL_REQUEST:
            lines.append(f"- {t.name} (SQL Cell) {t.title}: columns {cols}")
        else:
            lines.append(f"- {t.name} ({t.title}): columns {cols}; from request: {t.request!r}")
    return "\n".join(lines) + "\n"
