"""Only the AI Cell's row cap travels in MCP request _meta: no exchange id, and nothing in the launch environment or a tool schema."""

import json

from conftest import ScriptedLLM, TextChunk, collect, in_process_transport, last_tool_result, tool_call
from mcp import ClientSession

from flood_agent.tool_outcome import decode, project

CAP_KEY = "flood_agent/max_result_rows"


async def _raw_calls(agent, calls):
    """Call the MCP server directly: [(tool, args, meta)] -> parsed results (Query Metadata)."""
    out = []
    async with in_process_transport(agent.server_settings()) as (r, w), ClientSession(r, w) as session:
        await session.initialize()
        for name, args, meta in calls:
            res = await session.call_tool(name, args, meta=meta)
            parsed = json.loads(res.content[0].text)
            # A codec tool returns an outcome; what the LLM would be sent is its projection.
            out.append(json.loads(project(decode(res.content[0].text))) if "outcome" in parsed else parsed)
    return out


async def test_reads_and_displays_are_not_scoped_to_any_notebook(make_agent):
    results = []
    llm = ScriptedLLM(
        [tool_call("run_query", sql="SELECT dport FROM flows", title="t")],
        lambda m: results.append(last_tool_result(m)) or [TextChunk("done")],
    )
    agent = await make_agent(llm)
    notebook = agent.notebooks.create_notebook()
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
    async with in_process_transport(agent.server_settings()) as (r, w), ClientSession(r, w) as session:
        await session.initialize()
        for meta in ({}, {CAP_KEY: 0}, {CAP_KEY: "10"}):
            res = await session.call_tool("run_query", {"sql": "SELECT 1", "title": "t"}, meta=meta)
            assert res.isError
