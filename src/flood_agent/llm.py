"""The LLM client interface and its OpenRouter implementation.

The Agent depends only on the `LLMClient` protocol, so tests inject a scripted fake.
"""

import json
from dataclasses import dataclass
from typing import AsyncIterator, Protocol

import httpx
from pydantic import BaseModel, Field

OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"


class ModelSpec(BaseModel, frozen=True):
    """One entry of the curated model list (`[[models]]` in defaults.toml)."""

    id: str  # the OpenRouter model id
    label: str
    context_window: int = Field(ge=1)  # tokens
    efforts: list[str] = []  # Effort levels it offers; empty means no Effort control
    default_effort: str | None = None


@dataclass
class TextChunk:
    text: str


@dataclass
class ToolCallChunk:
    """A complete tool call (arguments are a JSON string)."""

    id: str
    name: str
    arguments: str


@dataclass
class UsageChunk:
    """Token counts and cost for one LLM step (cost in USD, None if the provider omits it)."""

    prompt_tokens: int = 0
    completion_tokens: int = 0
    cost: float | None = None
    cached_tokens: int = 0
    cache_write_tokens: int = 0


Chunk = TextChunk | ToolCallChunk | UsageChunk


class LLMClient(Protocol):
    def stream_chat(self, messages: list[dict], tools: list[dict], reasoning: dict | None = None,
                    session_id: str | None = None) -> AsyncIterator[Chunk]: ...
    # `reasoning` is set only when the user picked a non-default Effort; `session_id` names the conversation.


class LLMError(Exception):
    """The provider reported an error, or the response stream was cut short or malformed.
    The message is safe to show the user."""


SESSION_ID_PREFIXES = ("anthropic/", "openai/", "qwen/", "deepseek/", "z-ai/")


def caching_fields(model: str, session_id: str | None = None) -> dict:
    """Extra request-body fields a model needs for prompt caching, keyed on model-ID prefix.
    Claude only caches when the request carries cache_control. Claude, GPT-5.6, Qwen and GLM hit
    more often when OpenRouter can route a conversation to one endpoint by `session_id`;
    DeepSeek caches on every endpoint, so it is harmless there.

    Claude also prefers Anthropic's own endpoint. Measured on OpenRouter (#65): Google Vertex cannot
    read a cache entry written a few seconds earlier, so every step of a tool-call turn re-pays the whole
    prompt; Anthropic's endpoint reads it at once, about 13 times cheaper. It is a preference, not a pin:
    fallbacks stay on, so an account that does not allow Anthropic gets an uncached answer, not an error."""
    fields: dict = {}
    if model.startswith("anthropic/"):
        fields["cache_control"] = {"type": "ephemeral"}
        fields["provider"] = {"order": ["anthropic"]}
    if session_id and model.startswith(SESSION_ID_PREFIXES):
        fields["session_id"] = session_id
    return fields


class OpenRouterClient:
    """OpenAI-compatible streaming chat completions with tool calling."""

    def __init__(self, api_key: str, model: str, url: str = OPENROUTER_URL,
                 transport: httpx.AsyncBaseTransport | None = None):
        self.api_key, self.model, self.url, self.transport = api_key, model, url, transport

    async def stream_chat(self, messages, tools, reasoning: dict | None = None,
                          session_id: str | None = None):
        body = {"model": self.model, "messages": messages, "tools": tools, "stream": True,
            "stream_options": {"include_usage": True}, "usage": {"include": True}}
        body.update(caching_fields(self.model, session_id))
        if reasoning:
            body["reasoning"] = reasoning
        headers = {"Authorization": f"Bearer {self.api_key}"}
        pending: dict[int, dict] = {}
        usage = None
        complete = False  # saw [DONE] or a finish reason
        async with httpx.AsyncClient(timeout=120, transport=self.transport) as client:
            async with client.stream("POST", self.url, json=body, headers=headers) as resp:
                resp.raise_for_status()
                async for line in resp.aiter_lines():
                    if not line.startswith("data:"):
                        continue
                    payload = line[5:].strip()
                    if payload == "[DONE]":
                        complete = True
                        break
                    try:
                        obj = json.loads(payload)
                    except ValueError:
                        raise LLMError("The model provider sent a malformed response.") from None
                    if obj.get("error"):
                        raise LLMError(_describe_error(obj["error"]))
                    if obj.get("usage"):
                        u = obj["usage"]
                        details = u.get("prompt_tokens_details") or {}
                        usage = UsageChunk(u.get("prompt_tokens", 0), u.get("completion_tokens", 0), u.get("cost"),
                                           details.get("cached_tokens") or 0, details.get("cache_write_tokens") or 0)
                    choices = obj.get("choices") or []
                    if not choices:
                        continue
                    if choices[0].get("finish_reason") == "error":
                        raise LLMError("The model provider ended the response with an error.")
                    if choices[0].get("finish_reason"):
                        complete = True
                    delta = choices[0].get("delta", {})
                    if delta.get("content"):
                        yield TextChunk(delta["content"])
                    for tc in delta.get("tool_calls") or []:
                        slot = pending.setdefault(tc.get("index", 0), {"id": "", "name": "", "args": ""})
                        slot["id"] = tc.get("id") or slot["id"]
                        fn = tc.get("function", {})
                        slot["name"] = fn.get("name") or slot["name"]
                        slot["args"] += fn.get("arguments") or ""
        if not complete:
            raise LLMError("The model's response ended before it was complete. Please try again.")
        for _, slot in sorted(pending.items()):
            if not slot["id"] or not slot["name"] or not _is_json(slot["args"] or "{}"):
                raise LLMError("The model's response was cut off in the middle of a tool call. Please try again.")
            yield ToolCallChunk(slot["id"], slot["name"], slot["args"] or "{}")
        if usage:
            yield usage


def _is_json(s: str) -> bool:
    try:
        json.loads(s)
    except ValueError:
        return False
    return True


def _describe_error(err) -> str:
    msg = err.get("message") if isinstance(err, dict) else err
    return f"The model provider reported an error: {str(msg)[:300]}" if msg else "The model provider reported an error."
