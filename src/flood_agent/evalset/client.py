"""Usage/cost capture for the eval runner.

`UsageMeter` wraps any LLM client (the production `OpenRouterClient`, or a scripted fake in
tests), passes every chunk through unchanged, and accumulates the `UsageChunk`s the client
emits. Nothing extra reaches the LLM.
"""

from flood_agent.llm import UsageChunk


class UsageMeter:
    def __init__(self, inner):
        self.inner = inner
        self.total = UsageChunk()

    @property
    def usage(self) -> dict:
        return self.total.to_dict()

    async def stream_chat(self, messages, tools, reasoning=None, session_id=None):
        async for chunk in self.inner.stream_chat(messages, tools, reasoning=reasoning, session_id=session_id):
            if isinstance(chunk, UsageChunk):
                self._add(chunk)
            yield chunk

    def _add(self, u: UsageChunk) -> None:
        self.total = UsageChunk.total([self.total, u])
