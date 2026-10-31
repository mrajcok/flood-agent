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

from flood_defender.llm import Chunk, TextChunk, ToolCallChunk, UsageChunk

CANNED_PROMPTS_FILE = Path(__file__).with_name("canned_prompts.yaml")
CHUNK_SIZE = 8
CHUNK_DELAY_S = 0.02
_PLACEHOLDER = re.compile(r"^\{result_table\}$")


@dataclass
class CannedPrompt:
    id: str
    prompt: str
    steps: list[dict]
    expect: dict = field(default_factory=dict)
    after: str | None = None


def load_canned_prompts(path: Path = CANNED_PROMPTS_FILE) -> list[CannedPrompt]:
    return [CannedPrompt(**entry) for entry in yaml.safe_load(path.read_text())]


class FakeClient:
    def __init__(self, canned_prompts: list[CannedPrompt], chunk_delay_s: float = CHUNK_DELAY_S):
        self.canned_prompts, self.chunk_delay_s = canned_prompts, chunk_delay_s

    async def stream_chat(self, messages: list[dict], tools: list[dict]) -> AsyncIterator[Chunk]:
        last_user = max(i for i, m in enumerate(messages) if m["role"] == "user")
        canned = next((c for c in self.canned_prompts if c.prompt == messages[last_user]["content"]), None)
        step_index = sum(1 for m in messages[last_user:] if m["role"] == "assistant")
        step: dict
        if canned is None:
            step = {"text": self._unmatched_reply()}
        else:
            step = canned.steps[step_index]
        async for text in self._stream(step.get("text", "")):
            yield TextChunk(text)
        for n, call in enumerate(step.get("tool_calls", [])):
            args = self._resolve(call.get("args", {}), messages)
            yield ToolCallChunk(f"fake-{step_index}-{n}", call["name"], json.dumps(args))
        yield UsageChunk(prompt_tokens=0, completion_tokens=0, cost=0)

    async def _stream(self, text: str) -> AsyncIterator[str]:
        for i in range(0, len(text), CHUNK_SIZE):
            if i:
                await asyncio.sleep(self.chunk_delay_s)
            yield text[i : i + CHUNK_SIZE]

    def _unmatched_reply(self) -> str:
        listing = "\n".join(f"- {c.prompt}" for c in self.canned_prompts)
        return f"I'm in Fake Mode: no LLM is running. Pick one of the Canned Prompts:\n{listing}"

    def _resolve(self, args: dict, messages: list[dict]) -> dict:
        """Replace `{result_table}` with the latest successful run_query Result Table."""
        out = {}
        for key, value in args.items():
            if isinstance(value, str) and _PLACEHOLDER.match(value):
                value = _latest_result_table(messages)
            out[key] = value
        return out


def _latest_result_table(messages: list[dict]) -> str:
    for m in reversed(messages):
        if m["role"] == "tool":
            meta = json.loads(m["content"])
            if meta.get("success") and "result_table" in meta:
                return meta["result_table"]
    raise ValueError("no earlier Result Table in this Conversation")
