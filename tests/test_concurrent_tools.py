"""Tool calls from different Conversations run concurrently on the shared MCP server."""

import asyncio
import time

from conftest import ScriptedLLM, TextChunk, collect, tool_call

from flood_defender.agent import Conversation

SLOW_SQL = (
    "WITH RECURSIVE c(x) AS (SELECT 1 UNION ALL SELECT x+1 FROM c) SELECT count(*) AS n FROM c"
)
TIMEOUT_S = 3


class RoutedLLM(ScriptedLLM):
    """Scripts keyed by the turn's user message, so concurrent turns cannot steal each other's steps."""

    def __init__(self, scripts: dict[str, list]):
        super().__init__()
        self.scripts = scripts

    async def stream_chat(self, messages, tools):
        user = next(m["content"] for m in messages if m["role"] == "user")
        has_result = any(m["role"] == "tool" for m in messages)
        chunks = [TextChunk("done")] if has_result else self.scripts[user]
        for c in chunks:
            yield c


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
    await asyncio.sleep(0.5)  # let the slow query start
    await turn("fast")
    assert not slow.done(), "the slow query should still be running"
    assert finished["fast"] < TIMEOUT_S - 1
    await slow
    assert finished["slow"] >= TIMEOUT_S - 0.5
