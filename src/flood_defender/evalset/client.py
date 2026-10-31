"""Usage/cost capture for the eval runner.

`UsageMeter` wraps any LLM client (the production `OpenRouterClient`, or a scripted fake in
tests), passes every chunk through unchanged, and accumulates the `UsageChunk`s the client
emits. Nothing extra reaches the LLM.
"""

from flood_defender.llm import UsageChunk


class UsageMeter:
    def __init__(self, inner):
        self.inner = inner
        self.usage: dict = {"cost": None, "prompt_tokens": 0, "completion_tokens": 0}

    async def stream_chat(self, messages, tools):
        async for chunk in self.inner.stream_chat(messages, tools):
            if isinstance(chunk, UsageChunk):
                self._add(chunk)
            yield chunk

    def _add(self, u: UsageChunk) -> None:
        self.usage["prompt_tokens"] += u.prompt_tokens or 0
        self.usage["completion_tokens"] += u.completion_tokens or 0
        if u.cost is not None:
            self.usage["cost"] = (self.usage["cost"] or 0.0) + float(u.cost)
