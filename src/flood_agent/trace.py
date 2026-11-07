"""The per-turn Trace: what passed between the Agent and the LLM, for the user only.

A Trace is a plain JSON-able dict: turn-level `model` and `system_prompt`, an ordered list of
`entries`, and `totals`. It is stored in `turns.trace` and rendered by `templates/trace.html`.
It is never part of any message sent to the LLM.

Entry kinds: llm_step, tool_call, query_error, result_table, chart_spec, flag
(`flag`: {"type": "ip"|"number", "token": str}, one per unseen value in a completed reply).
"""

import time

from flood_agent.llm import UsageChunk
from flood_agent.mcp_protocol import QUERY_TOOLS


class TraceBuilder:
    def __init__(self, model: str):
        self.trace: dict = {"version": 1, "model": model, "system_prompt": None, "entries": []}

    def add(self, kind: str, **fields) -> dict:
        entry = {"kind": kind, **fields}
        self.trace["entries"].append(entry)
        return entry

    def llm_step(self, system_prompt: str, text: str, calls: list, usage, started: float,
                 request_bytes: int | None = None) -> None:
        """`request_bytes`: the size of the messages and tool definitions sent to the model."""
        if self.trace["system_prompt"] is None:
            self.trace["system_prompt"] = system_prompt
        extra = {} if system_prompt == self.trace["system_prompt"] else {"system_prompt": system_prompt}
        self.add(
            "llm_step",
            raw_response={
                "content": text,
                "tool_calls": [{"id": c.id, "name": c.name, "arguments": c.arguments} for c in calls],
            },
            usage=None
            if usage is None
            else usage.to_dict(),
            duration_ms=elapsed_ms(started),
            request_bytes=request_bytes,
            **extra,
        )

    def tool_call(self, name: str, arguments: dict, query_metadata: str, started: float,
                  executed_sql: str | None = None) -> None:
        """`query_metadata` is the exact tool message content sent to the LLM."""
        fields = {"sql": arguments.get("sql"), "title": arguments.get("title")} if name in QUERY_TOOLS else {}
        if executed_sql:
            fields["executed_sql"] = executed_sql
        self.add("tool_call", name=name, arguments=arguments, query_metadata=query_metadata,
                 duration_ms=elapsed_ms(started), **fields)

    def result_table(self, name: str, row_count: int | None, columns: list | None) -> None:
        self.add("result_table", name=name, row_count=row_count, columns=columns)

    def finish(self) -> dict:
        steps = [e for e in self.trace["entries"] if e["kind"] == "llm_step"]
        usages = [e["usage"] for e in steps if e["usage"]]
        total = UsageChunk.total(usages)
        self.trace["totals"] = {
            "llm_steps": len(steps),
            **{k: v for k, v in total.to_dict().items() if k != "cost"},
            "cost": None if total.cost is None else round(total.cost, 10),
            "duration_ms": sum(e["duration_ms"] for e in self.trace["entries"] if "duration_ms" in e),
        }
        return self.trace


def split_system_prompt(prompt: str | None) -> tuple[str, str, str]:
    """Split a system prompt into (instructions, Semantic Layer, rest) for display.

    The rest is everything from the Result Tables section on, which can differ between turns.
    Prompts without the expected headings land wholly in the first part.
    """
    prompt = prompt or ""
    head, sep, tail = prompt.partition("# Semantic Layer\n")
    if not sep:
        return prompt, "", ""
    layer, rt_sep, rest = tail.partition("\n# Result Tables")
    return head, "# Semantic Layer\n" + layer, (rt_sep + rest).lstrip("\n")


def context_tokens(trace: dict) -> int | None:
    """The prompt tokens of the latest LLM call in a Trace: how full the context was when it was sent."""
    for e in reversed(trace["entries"]):
        if e["kind"] == "llm_step" and e["usage"]:
            return e["usage"]["prompt_tokens"]
    return None


def short_model(model: str) -> str:
    """A model id without its vendor prefix, for display: "anthropic/claude-haiku-5.5" -> "Haiku-5.5"."""
    name = model.rpartition("/")[2]
    if name.startswith("claude-"):
        return name[len("claude-"):].capitalize()
    return name


def elapsed_ms(started: float) -> int:
    return int((time.monotonic() - started) * 1000)
