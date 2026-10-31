"""The per-turn Trace: what passed between the Agent and the LLM, for the user only.

A Trace is a plain JSON-able dict: turn-level `model` and `system_prompt`, an ordered list of
`entries`, and `totals`. It is stored in `turns.trace` and rendered by `templates/trace.html`.
It is never part of any message sent to the LLM.

Entry kinds: llm_step, tool_call, query_error, result_table, chart_spec, flag
(`flag`: {"type": "ip"|"number", "token": str}, one per unseen value in a completed reply).
"""

import time


class TraceBuilder:
    def __init__(self, model: str):
        self.trace: dict = {"version": 1, "model": model, "system_prompt": None, "entries": []}

    def add(self, kind: str, **fields) -> dict:
        entry = {"kind": kind, **fields}
        self.trace["entries"].append(entry)
        return entry

    def llm_step(self, system_prompt: str, text: str, calls: list, usage, started: float) -> None:
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
            else {"prompt_tokens": usage.prompt_tokens, "completion_tokens": usage.completion_tokens,
                  "cost": usage.cost},
            duration_ms=elapsed_ms(started),
            **extra,
        )

    def tool_call(self, name: str, arguments: dict, query_metadata: str, started: float,
                  executed_sql: str | None = None) -> None:
        """`query_metadata` is the exact tool message content sent to the LLM."""
        fields = {"sql": arguments.get("sql"), "title": arguments.get("title")} if name == "run_query" else {}
        if executed_sql:
            fields["executed_sql"] = executed_sql
        self.add("tool_call", name=name, arguments=arguments, query_metadata=query_metadata,
                 duration_ms=elapsed_ms(started), **fields)

    def result_table(self, name: str, row_count: int, columns: list) -> None:
        self.add("result_table", name=name, row_count=row_count, columns=columns)

    def finish(self) -> dict:
        steps = [e for e in self.trace["entries"] if e["kind"] == "llm_step"]
        usages = [e["usage"] for e in steps if e["usage"]]
        costs = [u["cost"] for u in usages if u["cost"] is not None]
        self.trace["totals"] = {
            "llm_steps": len(steps),
            "prompt_tokens": sum(u["prompt_tokens"] for u in usages),
            "completion_tokens": sum(u["completion_tokens"] for u in usages),
            "cost": round(sum(costs), 10) if costs else None,
            "duration_ms": sum(e["duration_ms"] for e in self.trace["entries"] if "duration_ms" in e),
        }
        return self.trace


def elapsed_ms(started: float) -> int:
    return int((time.monotonic() - started) * 1000)
