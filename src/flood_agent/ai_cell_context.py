"""The AI Cell context: what an AI Cell is sent from the Cells above it, and which AI Cells are Stale.

Pure over cell data (the dicts `NotebookStore.cells` returns, in order, each with its Draft and latest Run): no
store, no LLM. Messages are built from what each Cell ran, never from Drafts (ADR 0007); a SQL Cell's message is
stored at its Run, so what Cells below are sent stays frozen there. No telemetry rows get in (ADR 0001).

The system prompt is a `SystemPrompt` of three named sections, always rendered in ADR 0007 order: fixed
instructions, then the Semantic Layer, then the Result Tables last, because they are the only part that changes
between steps. `parse_system_prompt` is the inverse, for the Trace, which stores the prompt as a plain string."""

import hashlib
import json
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Callable

from flood_agent import table_names
from flood_agent.cell_kinds import AI, MARKDOWN

if TYPE_CHECKING:  # annotations only: no import-time coupling to the Catalog
    from flood_agent.catalog import Table


MARKDOWN_NOTE_LABEL = "[Markdown note]"  # prefixes a ticked Markdown Cell's text
SQL_CELL_LABEL = "[SQL Cell, run by the user]"  # prefixes a SQL Cell's stored message
SHOW_CELL_LABEL = "[SHOW Cell, run by the user]"  # prefixes a SHOW Cell's stored message
STOPPED_NOTE = "*Stopped by user.*"  # appended to an answer streamed before the user stopped the Run
STOPPED_BELOW = "(stopped by the user)"  # how AI Cells below are sent a stopped Run, of either Cell kind


@dataclass(frozen=True)
class AICellContext:
    messages: list[dict]  # from the Cells above, in order
    above: list[tuple[str, str]]  # (cell id, run id) of each AI Cell above that has run
    fingerprint: str  # of `messages`; stored with the Run to detect Stale


def fingerprint(messages: list[dict]) -> str:
    return hashlib.sha256(json.dumps(messages, sort_keys=True).encode()).hexdigest()


def context_above(cells: list[dict], cell_id: str) -> AICellContext:
    """What the AI Cell `cell_id` is sent from the Cells above it."""
    messages: list[dict] = []
    above: list[tuple[str, str]] = []
    for other in cells:
        if other["id"] == cell_id:
            break
        if other["kind"] == MARKDOWN:
            text = other["draft"].get("text", "").strip()
            if other["draft"].get("send_to_ai") and (text or other["draft"].get("displays")):
                messages.append({"role": "user", "content": markdown_note(other["draft"])})
        elif other["run"]:
            messages += what_ran(other["run"])
            above.append((other["id"], other["run"]["id"]))
    return AICellContext(messages, above, fingerprint(messages))


def listed_result_tables(
    above: list[tuple[str, str]], cell_id: str, run_id: str, tables_of_run: "Callable[[str, str], list[Table]]",
) -> "list[Table]":
    """The Result Tables the system prompt lists: those of the AI Cells above (their current Runs, `above`) plus
    the Cell's own current Run, oldest first then by name. `tables_of_run(cell_id, run_id)` looks up the live
    tables; asked afresh each time because a step can make a Result Table partway through a Run."""
    tables = [t for c, r in above for t in tables_of_run(c, r)]
    tables += tables_of_run(cell_id, run_id)
    return sorted(tables, key=lambda t: (t.created_at_ms, t.name))


def stale_cells(cells: list[dict]) -> set[str]:
    """The ids of the AI Cells that are Stale: the context each would be sent now differs from the one its latest
    Run was sent. SQL Cells (which store no fingerprint), cells that never ran, cells still running and Runs with
    no stored fingerprint are never Stale."""
    return {cell["id"] for i, cell in enumerate(cells) if is_stale(cell, cells[:i])}


def is_stale(cell: dict, above: list[dict]) -> bool:
    """Whether one Cell is Stale (see `stale_cells`), from it and the Cells `above` it only."""
    run = cell["run"]
    if cell["kind"] != AI or not run or run["status"] == "running" or not run["context_fingerprint"]:
        return False
    return context_above(above, cell["id"]).fingerprint != run["context_fingerprint"]


def markdown_note(draft: dict) -> str:
    """What an AI Cell is sent for a ticked Markdown Cell: its text, then one line per embedded Display naming
    its kind and table. Never rows or a chart's data (ADR 0001); the same draft always gives the same note."""
    lines = [f"{MARKDOWN_NOTE_LABEL} {draft.get('text', '').strip()}".rstrip()]
    lines += [f"[Display: {d['kind']} {d['table']}]" for d in draft.get("displays") or []]
    return "\n".join(lines)


def without_stop_note(text: str) -> str:
    """An answer without the app's stop note, which the page and the Cells below say in their own words."""
    return text.removesuffix(STOPPED_NOTE).rstrip()


def what_ran(run: dict) -> list[dict]:
    """A finished Run's messages as the Cells below are sent them: a stopped or failed Run counts as having
    run, with what it streamed plus `STOPPED_BELOW` or "failed: <the Run's value-free error>"."""
    messages = repair_history(run["messages"])
    if run["status"] not in ("stopped", "failed"):
        return messages
    last = messages[-1] if messages and messages[-1]["role"] == "assistant" else None
    text = (last.get("content") or "") if last else ""
    if run["status"] == "stopped":
        note = STOPPED_BELOW
        text = without_stop_note(text)
    else:
        note = f"failed: {run['error']}"
        if text == run["error"]:
            text = ""  # the error message was recorded as the answer: say it once
    if last:
        messages = messages[:-1]
    return messages + [{"role": "assistant", "content": f"{text}\n\n{note}" if text else note}]


def repair_history(messages: list[dict]) -> list[dict]:
    """Drop assistant tool_calls messages that lack a tool result for every call id (and the
    partial results after them), and orphan tool messages, so a corrupt saved history
    never makes the provider reject every later request."""
    out: list[dict] = []
    i = 0
    while i < len(messages):
        m = messages[i]
        if m["role"] == "assistant" and m.get("tool_calls"):
            j = i + 1
            answered = set()
            while j < len(messages) and messages[j]["role"] == "tool":
                answered.add(messages[j].get("tool_call_id"))
                j += 1
            if {tc["id"] for tc in m["tool_calls"]} <= answered:
                out.extend(messages[i:j])
            i = j
        elif m["role"] == "tool":
            i += 1
        else:
            out.append(m)
            i += 1
    return out


SEMANTIC_LAYER_HEADING = "# Semantic Layer\n"
RESULT_TABLES_HEADING = "# Result Tables"
_SEMANTIC_LAYER_LINE = re.compile(f"^{re.escape(SEMANTIC_LAYER_HEADING)}", re.MULTILINE)


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
    heading = _SEMANTIC_LAYER_LINE.search(prompt)  # a heading line only, not the same text mid-line
    if not heading:
        return SystemPrompt(prompt, "", "")
    head, tail = prompt[: heading.start()], prompt[heading.end() :]
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
        cols = ", ".join(
            f"{c['name']} {c['type']}" + (" (restricted)" if c["name"] in t.restricted_columns else "")
            for c in t.columns)
        if t.request == table_names.SQL_CELL_REQUEST:
            lines.append(f"- {t.name} (SQL Cell) {t.title}: columns {cols}")
        else:
            lines.append(f"- {t.name} ({t.title}): columns {cols}; from request: {t.request!r}")
    return "\n".join(lines) + "\n"
