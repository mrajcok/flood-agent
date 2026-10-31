"""Fake Mode: a scripted stand-in for the LLM, driven by Canned Prompts.

`FakeClient` implements the `LLMClient` protocol. It reads only what a real LLM would see
(user messages and Query Metadata in tool results), so ADR 0001 holds. The script is chosen by
exact match on the latest user message; the step within the turn is the number of assistant
messages since that message.
"""

import asyncio
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import AsyncIterator

import yaml

from flood_defender.llm import Chunk, LLMError, TextChunk, ToolCallChunk, UsageChunk

CANNED_PROMPTS_FILE = Path(__file__).with_name("canned_prompts.yaml")
CHUNK_SIZE = 8
CHUNK_DELAY_S = 0.02
SCRIPT_ENDED = "Fake Mode: script ended."
_PLACEHOLDER = re.compile(r"\{result_table(?::(-\d+))?\}")


class NoResultTable(Exception):
    """A placeholder (or a follow-up's parent) has no successful Result Table to draw on."""


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
    def __init__(self, canned_prompts: list[CannedPrompt], chunk_delay_s: float = CHUNK_DELAY_S):
        self.canned_prompts, self.chunk_delay_s = canned_prompts, chunk_delay_s

    async def stream_chat(self, messages: list[dict], tools: list[dict]) -> AsyncIterator[Chunk]:
        last_user = max(i for i, m in enumerate(messages) if m["role"] == "user")
        canned = next((c for c in self.canned_prompts if c.prompt == messages[last_user]["content"]), None)
        step_index = sum(1 for m in messages[last_user:] if m["role"] == "assistant")
        if canned is None:
            step = Step(text=self._unmatched_reply())
        elif step_index < len(canned.steps):
            step = canned.steps[step_index]
        else:
            step = Step(text=SCRIPT_ENDED)
        try:
            scope = self._scope(canned, messages, last_user)
            calls = [(c.name, self._arguments(c, scope)) for c in step.tool_calls]
        except NoResultTable:  # ask for the parent instead of sending a broken call
            parent = parent_of(canned, self.canned_prompts)
            what = f"*{parent.prompt}*" if parent else "an earlier prompt"
            step = Step(text=f"Fake Mode: run {what} first in this Conversation")
            calls = []
        async for text in self._stream(step.text):
            yield TextChunk(text)
        for n, (name, arguments) in enumerate(calls):
            yield ToolCallChunk(f"fake-{step_index}-{n}", name, arguments)
        if step.error is not None:
            raise LLMError(step.error)
        yield UsageChunk(prompt_tokens=0, completion_tokens=0, cost=0)

    def _scope(self, canned: CannedPrompt | None, messages: list[dict], last_user: int) -> list[dict]:
        """The messages placeholders resolve from. A follow-up draws only on what came after its
        parent's latest run in this Conversation; no such run with a Result Table means NoResultTable."""
        parent = parent_of(canned, self.canned_prompts)
        if canned is None or canned.after is None:
            return messages
        if parent is None:
            raise NoResultTable
        ran = [i for i, m in enumerate(messages[:last_user]) if m["role"] == "user" and m["content"] == parent.prompt]
        if not ran:
            raise NoResultTable
        scope = messages[ran[-1] :]
        if not _result_tables(scope):
            raise NoResultTable
        return scope

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
        listing = "\n".join(f"- {c.prompt}" for c in self.canned_prompts)
        return f"I'm in Fake Mode: no LLM is running. Pick one of the Canned Prompts:\n{listing}"

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


def _result_tables(messages: list[dict]) -> list[str]:
    """Successful run_query Result Table names in the message history, oldest first."""
    names = []
    for m in messages:
        if m["role"] == "tool":
            meta = json.loads(m["content"])
            if meta.get("success") and "result_table" in meta:
                names.append(meta["result_table"])
    return names


def _result_table(messages: list[dict], index: int) -> str:
    """The Nth-latest (-1 = latest) successful run_query Result Table in the message history."""
    names = _result_tables(messages)
    if len(names) < -index:
        raise NoResultTable("no earlier Result Table in this Conversation")
    return names[index]
