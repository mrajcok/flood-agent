"""OpenRouter client for the eval runner: the production client plus usage/cost capture.

It sends exactly the same chat request as `OpenRouterClient` (plus `usage.include`, which
only asks OpenRouter to report token counts and cost back); nothing extra reaches the LLM.
`llm.py` is left untouched so this stays independent of the Agent's evolving client.
"""

import json

import httpx

from flood_defender.llm import OpenRouterClient, TextChunk, ToolCallChunk


class MeteredOpenRouterClient(OpenRouterClient):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.usage: dict = {"cost": None, "prompt_tokens": 0, "completion_tokens": 0}

    async def stream_chat(self, messages, tools):
        body = {
            "model": self.model, "messages": messages, "tools": tools, "stream": True,
            "usage": {"include": True},
        }
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
                    event = json.loads(payload)
                    if event.get("usage"):
                        self._add_usage(event["usage"])
                    choices = event.get("choices") or []
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

    def _add_usage(self, u: dict) -> None:
        self.usage["prompt_tokens"] += u.get("prompt_tokens") or 0
        self.usage["completion_tokens"] += u.get("completion_tokens") or 0
        if u.get("cost") is not None:
            self.usage["cost"] = (self.usage["cost"] or 0.0) + float(u["cost"])
