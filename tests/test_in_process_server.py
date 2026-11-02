"""Tests not marked `subprocess` run the MCP server in-process: same tools, no child process."""

import pytest
from conftest import ScriptedLLM, collect, last_tool_result, mcp_children, tool_call

from flood_agent.agent import TurnEnd
from flood_agent.llm import TextChunk


async def test_a_turn_runs_its_tools_without_starting_a_server_process(make_agent):
    llm = ScriptedLLM([tool_call("run_query", sql="SELECT dport FROM flows", title="t")], [TextChunk("done")])
    agent = await make_agent(llm)
    events = await collect(agent, "q")
    assert isinstance(events[-1], TurnEnd)
    assert last_tool_result(llm.calls[-1])["success"] is True
    assert mcp_children() == []


@pytest.mark.subprocess
async def test_a_subprocess_marked_test_gets_a_real_server_process(make_agent):
    await make_agent(ScriptedLLM())
    assert len(mcp_children()) == 1
