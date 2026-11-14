"""Only the AI Cell's row cap travels in MCP request _meta: no exchange id, and nothing in the launch environment or a tool schema."""

import json

import pytest
from conftest import ScriptedLLM, TextChunk, collect, in_process_transport, last_tool_result, mcp_children, tool_call
from mcp import ClientSession

CAP_KEY = "flood_agent/max_result_rows"


@pytest.mark.subprocess
async def test_launch_environment_has_no_exchange_id(make_agent):
    seen = {}

    def peek(messages):
        (pid,) = mcp_children()
        with open(f"/proc/{pid}/environ", "rb") as f:
            seen["env"] = dict(kv.split(b"=", 1) for kv in f.read().split(b"\0") if b"=" in kv)
        return [TextChunk("ok")]

    await collect(await make_agent(ScriptedLLM(peek)), "hi")
    assert b"TELEMETRY_DB" in seen["env"]
    assert b"CELL_ID" not in seen["env"] and b"EXCHANGE_ID" not in seen["env"]


async def test_no_tool_schema_mentions_an_exchange_id_or_a_chat(make_agent):
    seen = {}

    class Spy(ScriptedLLM):
        async def stream_chat(self, messages, tools, reasoning: dict | None = None, session_id: str | None = None):
            seen["tools"] = json.dumps(tools).lower()
            async for c in super().stream_chat(messages, tools):
                yield c

    await collect(await make_agent(Spy([TextChunk("ok")])), "hi")
    assert "exchange_id" not in seen["tools"] and "any chat" not in seen["tools"] and "any notebook" in seen["tools"]
    assert "_meta" not in seen["tools"]


async def _raw_calls(agent, calls):
    """Call the MCP server directly: [(tool, args, meta)] -> parsed results."""
    out = []
    async with in_process_transport(agent._server_settings()) as (r, w), ClientSession(r, w) as session:
        await session.initialize()
        for name, args, meta in calls:
            res = await session.call_tool(name, args, meta=meta)
            out.append(json.loads(res.content[0].text))
    return out


async def test_reads_and_displays_are_not_scoped_to_any_notebook(make_agent):
    results = []
    llm = ScriptedLLM(
        [tool_call("run_query", sql="SELECT dport FROM flows", title="t")],
        lambda m: results.append(last_tool_result(m)) or [TextChunk("done")],
    )
    agent = await make_agent(llm)
    notebook = agent.create_notebook()
    await collect(agent, "q", notebook)
    table = results[0]["result_table"]
    q = {"sql": f"SELECT * FROM {table}", "title": "again"}
    own, missing, wrong, empty = await _raw_calls(
        agent,
        [
            ("run_query", q, {CAP_KEY: 1000}),
            ("run_query", q, {CAP_KEY: 1000, "flood_agent/exchange_id": "old"}),  # an old key is simply ignored
            ("show_table", {"result_table": table}, {}),
            ("show_table", {"result_table": table}, {"x": ""}),
        ],
    )
    assert own["success"]
    assert missing["success"]  # reads are not scoped to a notebook (ADR 0003)
    assert wrong["success"] and empty["success"]  # nor are Displays of a live Result Table


async def test_run_query_without_a_row_cap_in_meta_is_refused(make_agent):
    agent = await make_agent(ScriptedLLM())
    async with in_process_transport(agent._server_settings()) as (r, w), ClientSession(r, w) as session:
        await session.initialize()
        for meta in ({}, {CAP_KEY: 0}, {CAP_KEY: "10"}):
            res = await session.call_tool("run_query", {"sql": "SELECT 1", "title": "t"}, meta=meta)
            assert res.isError
