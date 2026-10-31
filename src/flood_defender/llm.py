"""The LLM client interface and its OpenRouter implementation.

The Agent depends only on the `LLMClient` protocol, so tests inject a scripted fake.
"""

import json
import os
from dataclasses import dataclass
from typing import AsyncIterator, Protocol

import httpx

DEFAULT_MODEL = "anthropic/claude-sonnet-5.5"
OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"


def model_from_env() -> str:
    return os.environ.get("OPENROUTER_MODEL") or DEFAULT_MODEL


@dataclass
class TextChunk:
    text: str


@dataclass
class ToolCallChunk:
    """A complete tool call (arguments are a JSON string)."""

    id: str
    name: str
    arguments: str


Chunk = TextChunk | ToolCallChunk


class LLMClient(Protocol):
    def stream_chat(self, messages: list[dict], tools: list[dict]) -> AsyncIterator[Chunk]: ...


class OpenRouterClient:
    """OpenAI-compatible streaming chat completions with tool calling."""

    def __init__(self, api_key: str, model: str, url: str = OPENROUTER_URL):
        self.api_key, self.model, self.url = api_key, model, url

    @classmethod
    def from_env(cls) -> "OpenRouterClient":
        key = os.environ.get("OPENROUTER_API_KEY")
        if not key:
            raise RuntimeError("OPENROUTER_API_KEY is not set")
        return cls(key, model_from_env())

    async def stream_chat(self, messages, tools):
        body = {"model": self.model, "messages": messages, "tools": tools, "stream": True}
        headers = {"Authorization": f"Bearer {self.api_key}"}
        pending: dict[int, dict] = {}
        async with httpx.AsyncClient(timeout=120) as client:
            async with client.stream("POST", self.url, json=body, headers=headers) as resp:
                resp.raise_for_status()
                async for line in resp.aiter_lines():
                    if not line.startswith("data:"):
                        continue
                    payload = line[5:].strip()
                    if payload == "[DONE]":
                        break
                    choices = json.loads(payload).get("choices") or []
                    if not choices:
                        continue
                    delta = choices[0].get("delta", {})
                    if delta.get("content"):
                        yield TextChunk(delta["content"])
                    for tc in delta.get("tool_calls") or []:
                        slot = pending.setdefault(tc.get("index", 0), {"id": "", "name": "", "args": ""})
                        slot["id"] = tc.get("id") or slot["id"]
                        fn = tc.get("function", {})
                        slot["name"] = fn.get("name") or slot["name"]
                        slot["args"] += fn.get("arguments") or ""
        for _, slot in sorted(pending.items()):
            yield ToolCallChunk(slot["id"], slot["name"], slot["args"] or "{}")
