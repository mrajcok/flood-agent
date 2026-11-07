"""Tool calls from different Conversations run concurrently on the shared MCP server."""

import asyncio
import time

from conftest import ScriptedLLM, TextChunk, collect, tool_call

from flood_agent.agent import Conversation

SLOW_SQL = (
    "WITH RECURSIVE c(x) AS (SELECT 1 UNION ALL SELECT x+1 FROM c) SELECT count(*) AS n FROM c"
)
TIMEOUT_S = 1.0


class RoutedLLM(ScriptedLLM):
    """Scripts keyed by the turn's user message, so concurrent turns cannot steal each other's steps."""

    def __init__(self, scripts: dict[str, list]):
        super().__init__()
        self.scripts = scripts
        self.served: dict[str, asyncio.Event] = {name: asyncio.Event() for name in scripts}

    async def stream_chat(self, messages, tools, reasoning: dict | None = None, session_id: str | None = None):
        user = next(m["content"] for m in messages if m["role"] == "user")
        has_result = any(m["role"] == "tool" for m in messages)
        chunks = [TextChunk("done")] if has_result else self.scripts[user]
        for c in chunks:
            yield c
        if not has_result:
            self.served[user].set()  # the turn's tool call is about to run


async def test_slow_query_does_not_block_other_conversations(make_agent):
    llm = RoutedLLM(
        {
            "slow": [tool_call("run_query", sql=SLOW_SQL, title="slow")],
            "fast": [tool_call("show_table", result_table="nope")],
        }
    )
    agent = await make_agent(llm, query_timeout_s=TIMEOUT_S)
    start = time.monotonic()
    finished = {}

    async def turn(message):
        await collect(agent, message, Conversation())
        finished[message] = time.monotonic() - start

    slow = asyncio.create_task(turn("slow"))
    await llm.served["slow"].wait()
    await asyncio.sleep(0.05)  # the tool call reaches the server's worker thread
    fast_start = time.monotonic()
    await turn("fast")
    assert not slow.done(), "the slow query should still be running"
    # Timed from its own start, so a slow start of the slow turn (CPU contention) cannot fail it.
    assert time.monotonic() - fast_start < TIMEOUT_S / 2
    await slow
    assert finished["slow"] >= TIMEOUT_S * 0.8
