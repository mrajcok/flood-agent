"""The AI Cell context: what an AI Cell is sent from the Cells above it, and which AI Cells are Stale.

Pure over cell data (the dicts `NotebookStore.cells` returns, in order, each with its Draft and latest Run): no
store, no LLM. Messages are built from what each Cell ran, never from Drafts (ADR 0007); a SQL Cell's message is
stored at its Run, so what Cells below are sent stays frozen there. No telemetry rows get in (ADR 0001)."""

import hashlib
import json
from dataclasses import dataclass

from flood_agent.notebooks import AI, MARKDOWN

MARKDOWN_NOTE_LABEL = "[Markdown note]"  # prefixes a ticked Markdown Cell's text
SQL_CELL_LABEL = "[SQL Cell, run by the user]"  # prefixes a SQL Cell's stored message
SHOW_CELL_LABEL = "[SHOW Cell, run by the user]"  # prefixes a SHOW Cell's stored message
STOPPED_NOTE = "*Stopped by user.*"  # appended to an answer streamed before the user stopped the Run


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


def stale_cells(cells: list[dict]) -> set[str]:
    """The ids of the AI Cells that are Stale: the context each would be sent now differs from the one its latest
    Run was sent. SQL Cells (which store no fingerprint), cells that never ran, cells still running and Runs with
    no stored fingerprint are never Stale."""
    stale = set()
    for cell in cells:
        run = cell["run"]
        if cell["kind"] == AI and run and run["status"] != "running" and run["context_fingerprint"]:
            if context_above(cells, cell["id"]).fingerprint != run["context_fingerprint"]:
                stale.add(cell["id"])
    return stale


def markdown_note(draft: dict) -> str:
    """What an AI Cell is sent for a ticked Markdown Cell: its text, then one line per embedded Display naming
    its kind and table. Never rows or a chart's data (ADR 0001); the same draft always gives the same note."""
    lines = [f"{MARKDOWN_NOTE_LABEL} {draft.get('text', '').strip()}".rstrip()]
    lines += [f"[Display: {d['kind']} {d['table']}]" for d in draft.get("displays") or []]
    return "\n".join(lines)


def what_ran(run: dict) -> list[dict]:
    """A finished Run's messages as the Cells below are sent them: a stopped or failed Run counts as having
    run, with what it streamed plus "(stopped by the user)" or "failed: <value-free error>"."""
    messages = repair_history(run["messages"])
    if run["status"] not in ("stopped", "failed"):
        return messages
    last = messages[-1] if messages and messages[-1]["role"] == "assistant" else None
    text = (last.get("content") or "") if last else ""
    if run["status"] == "stopped":
        note = "(stopped by the user)"
        text = text.removesuffix(STOPPED_NOTE).rstrip()
    else:
        errors = [e["message"] for e in (run["trace"] or {}).get("entries", []) if e["kind"] == "error"]
        note = f"failed: {errors[0] if errors else 'the Run did not finish'}"
        if errors and text == errors[0]:
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
