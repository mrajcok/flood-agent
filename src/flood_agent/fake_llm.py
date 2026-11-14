"""Fake Mode: a scripted stand-in for the LLM, driven by Canned Prompts.

`FakeClient` implements the `LLMClient` protocol. It reads only what a real LLM would see
(user messages and Query Metadata in tool results), so ADR 0001 holds. The script is chosen by
exact match on the latest user message; the step within the turn is the number of assistant
messages since that message.
"""

import asyncio
import json
import re
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import AsyncIterator

import yaml

from flood_agent.llm import Chunk, LLMError, TextChunk, ToolCallChunk, UsageChunk

CANNED_PROMPTS_FILE = Path(__file__).with_name("canned_prompts.yaml")
CHUNK_SIZE = 8
CHUNK_DELAY_S = 0.02
_COUNT_PLACEHOLDER = "{count}"  # in step text: the latest run_count_query count of this turn
_PLACEHOLDER = re.compile(r"\{result_table(?::(-\d+))?\}")


class ParentNotRun(Exception):
    """A follow-up was picked before its parent Canned Prompt ran in this notebook."""

    def __init__(self, parent: "CannedPrompt"):
        self.parent = parent


class QueryFailed(Exception):
    """The query a placeholder depends on failed, so its Result Table never existed."""

    def __init__(self, error: str | None):
        self.error = error


@dataclass(frozen=True)
class Call:
    name: str
    args: dict = field(default_factory=dict)
    raw_arguments: str | None = None  # deliberately malformed, sent as-is instead of `args`


@dataclass(frozen=True)
class Step:
    text: str = ""
    tool_calls: tuple[Call, ...] = ()
    error: str | None = None  # raised as the LLM error after the text streams
    if_previous_failed: str = ""  # reply with this text alone (no calls) if the turn's latest tool call failed


@dataclass(frozen=True)
class Expect:
    displays: tuple[str, ...] = ()
    failed_tool_calls: int = 0
    notice: str = ""
    turn_error: bool = False


@dataclass(frozen=True)
class CannedPrompt:
    id: str
    prompt: str
    steps: tuple[Step, ...]
    expect: Expect = Expect()
    after: str | None = None

    @property
    def fails_on_purpose(self) -> bool:
        return bool(self.expect.failed_tool_calls or self.expect.notice)


def _parse_step(raw: dict) -> Step:
    calls = tuple(Call(**c) for c in raw.get("tool_calls", []))
    return Step(**{**raw, "tool_calls": calls})


def _parse_canned_prompt(entry: dict) -> CannedPrompt:
    expect = Expect(**{**entry.get("expect", {}), "displays": tuple(entry.get("expect", {}).get("displays", []))})
    steps = tuple(_parse_step(s) for s in entry.get("steps", []))
    return CannedPrompt(**{**entry, "steps": steps, "expect": expect})


def load_canned_prompts(path: Path = CANNED_PROMPTS_FILE) -> list[CannedPrompt]:
    """Parse the YAML into typed prompts; unknown keys anywhere are a load error (TypeError)."""
    prompts = [_parse_canned_prompt(entry) for entry in yaml.safe_load(path.read_text())]
    ids = {c.id for c in prompts}
    for c in prompts:
        if c.after is not None and c.after not in ids:
            raise ValueError(f"Canned Prompt {c.id!r} has after: {c.after!r}, which is not a Canned Prompt id")
    return prompts


def parent_of(canned: CannedPrompt | None, canned_prompts: list[CannedPrompt]) -> CannedPrompt | None:
    """The Canned Prompt a follow-up (`after:`) depends on, or None."""
    if canned is None or canned.after is None:
        return None
    return next((c for c in canned_prompts if c.id == canned.after), None)


class FakeClient:
    def __init__(
        self, canned_prompts: list[CannedPrompt], chunk_delay_s: float = CHUNK_DELAY_S, telemetry_db: Path | None = None
    ):
        """`telemetry_db` is only named in the reply that suggests generating the data."""
        self.canned_prompts, self.chunk_delay_s, self.telemetry_db = canned_prompts, chunk_delay_s, telemetry_db

    async def stream_chat(self, messages: list[dict], tools: list[dict],
                          reasoning: dict | None = None, session_id: str | None = None) -> AsyncIterator[Chunk]:
        last_user = max(i for i, m in enumerate(messages) if m["role"] == "user")
        canned = next((c for c in self.canned_prompts if _same_prompt(c.prompt, messages[last_user]["content"])), None)
        step_index = sum(1 for m in messages[last_user:] if m["role"] == "assistant")
        if canned is None:
            step = Step(text=self._unmatched_reply())
        elif step_index < len(canned.steps):
            step = canned.steps[step_index]
        else:  # the script ended on tool calls (e.g. show_chart after its intro): nothing more to say
            step = Step()
        if step.if_previous_failed and _latest_tool_call_failed(messages[last_user:]):
            step = Step(text=step.if_previous_failed)
        try:  # don't send a broken call: say why there is nothing to show
            scope = self._scope(canned, messages, last_user)
            calls = [(c.name, self._arguments(c, scope)) for c in step.tool_calls]
            if _COUNT_PLACEHOLDER in step.text:
                step = replace(step, text=step.text.replace(_COUNT_PLACEHOLDER, _count(scope)))
        except ParentNotRun as e:
            step, calls = Step(text=_run_parent_first_reply(e.parent)), []
        except QueryFailed as e:
            step, calls = Step(text=self._query_failed_reply(e.error)), []
        async for text in self._stream(step.text):
            yield TextChunk(text)
        for n, (name, arguments) in enumerate(calls):
            yield ToolCallChunk(f"fake-{step_index}-{n}", name, arguments)
        if step.error is not None:
            raise LLMError(step.error)
        yield UsageChunk(prompt_tokens=0, completion_tokens=0, cost=0)

    def _scope(self, canned: CannedPrompt | None, messages: list[dict], last_user: int) -> list[dict]:
        """The messages placeholders resolve from: this turn's own queries, or for a follow-up,
        everything from its parent's latest run in this notebook."""
        parent = parent_of(canned, self.canned_prompts)
        if parent is None:
            return messages[last_user:]
        ran = [i for i, m in enumerate(messages[:last_user]) if m["role"] == "user" and _same_prompt(m["content"], parent.prompt)]
        if not ran:
            raise ParentNotRun(parent)
        return messages[ran[-1] :]

    def _query_failed_reply(self, error: str | None) -> str:
        """Quotes the (value-free) Query Metadata error; missing data is the usual cause in Fake Mode."""
        detail = f" ({error})" if error else ""
        path = self.telemetry_db or "<TELEMETRY_DB path>"
        return (
            f"Fake Mode: the query failed{detail}, so there is nothing to show. If the Telemetry Database "
            f"has not been generated yet, run: uv run python -m flood_agent.generator {path}"
        )

    def _arguments(self, call: Call, messages: list[dict]) -> str:
        if call.raw_arguments is not None:
            return call.raw_arguments
        return json.dumps(self._resolve(call.args, messages))

    async def _stream(self, text: str) -> AsyncIterator[str]:
        for i in range(0, len(text), CHUNK_SIZE):
            if i:
                await asyncio.sleep(self.chunk_delay_s)
            yield text[i : i + CHUNK_SIZE]

    def _unmatched_reply(self) -> str:
        return "I'm in Fake Mode (\"No model\"), so no language model is involved. Pick one of the canned prompts above the message box."

    def _resolve(self, node, messages: list[dict]):
        """Fill `{result_table}` / `{result_table:-2}` anywhere inside the arguments (nested
        dicts, lists, strings) from Query Metadata in earlier tool results."""
        if isinstance(node, str):
            return _PLACEHOLDER.sub(lambda m: _result_table(messages, int(m[1] or -1)), node)
        if isinstance(node, dict):
            return {k: self._resolve(v, messages) for k, v in node.items()}
        if isinstance(node, list):
            return [self._resolve(v, messages) for v in node]
        return node


def _same_prompt(a: str, b: str) -> bool:
    """Canned Prompts match exactly, except for surrounding spaces and one trailing period
    (so a message sent before the prompts lost their periods still matches)."""
    return a.strip().removesuffix(".").strip() == b.strip().removesuffix(".").strip()


def _latest_tool_call_failed(messages: list[dict]) -> bool:
    tool_messages = [m for m in messages if m["role"] == "tool"]
    return bool(tool_messages) and not json.loads(tool_messages[-1]["content"]).get("success")


def _run_parent_first_reply(parent: CannedPrompt) -> str:
    return f"Fake Mode: run *{parent.prompt}* first in an AI Cell above"


def _count(messages: list[dict]) -> str:
    """The latest run_count_query count in `messages`; a failed or missing one is QueryFailed."""
    results = _tool_results(messages, "run_count_query")
    if not results or not results[-1].get("success"):
        raise QueryFailed(results[-1].get("error") if results else None)
    return f"{results[-1]['count']:,}"


def _run_query_results(messages: list[dict]) -> list[dict]:
    """Query Metadata of every run_query result in `messages`, oldest first."""
    return _tool_results(messages, "run_query")


def _tool_results(messages: list[dict], tool: str) -> list[dict]:
    ids = {
        tc["id"]
        for m in messages
        if m["role"] == "assistant"
        for tc in m.get("tool_calls") or []
        if tc["function"]["name"] == tool
    }
    return [json.loads(m["content"]) for m in messages if m["role"] == "tool" and m.get("tool_call_id") in ids]


def _result_table(messages: list[dict], index: int) -> str:
    """The Nth-latest (-1 = latest) successful run_query Result Table in `messages`. If the latest
    query failed, a placeholder must not fall back to an older Result Table: that is QueryFailed."""
    results = _run_query_results(messages)
    if results and not results[-1].get("success"):
        raise QueryFailed(results[-1].get("error"))
    names = [r["result_table"] for r in results if r.get("success") and "result_table" in r]
    if len(names) < -index:
        raise QueryFailed(None)
    return names[index]
