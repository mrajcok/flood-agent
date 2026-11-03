"""describe_table and cross-Conversation reads of live Result Tables (ADR 0003), through the Agent turn seam."""

import json

from conftest import ScriptedLLM, all_telemetry_values, collect, last_tool_result, tool_call
from test_chaining_expiry import HOUR, Clock, run_first

from flood_agent.agent import Conversation
from flood_agent.llm import TextChunk, ToolCallChunk

__all__ = ["ScriptedLLM"]


async def describe_from_other_conversation(agent, llm, name):
    llm.steps = [[ToolCallChunk(id="c1", name="describe_table", arguments=json.dumps({"name": name}))], [TextChunk("ok")]]
    await collect(agent, f"describe {name}", Conversation())
    return last_tool_result(llm.calls[-1])


async def test_describe_table_reports_kind_columns_types_and_row_count_but_no_values(make_agent, telemetry_db):
    agent, llm, _, name = await run_first(make_agent)
    result = await describe_from_other_conversation(agent, llm, name)
    assert result["success"] is True
    assert result["kind"] == "result"
    assert result["name"] == name
    assert [c["name"] for c in result["columns"]] == ["dport", "total_bytes"]
    assert all(isinstance(c["type"], str) and c["type"] for c in result["columns"])
    assert result["row_count"] == agent.fetch_rows(name)["total"] > 0
    values = {v for v in all_telemetry_values(telemetry_db) if len(v) >= 4} | {"1111111", "555321"}
    text = json.dumps(result)
    assert not any(v in text for v in values)


async def test_describe_table_on_unknown_name_is_a_value_free_error(make_agent):
    agent, llm, _, _ = await run_first(make_agent)
    result = await describe_from_other_conversation(agent, llm, "no_such_table_20200101_000000")
    assert result["success"] is False
    assert "no such table" in result["error"].lower()
    assert "no_such_table_2020" not in result["error"]


async def test_describe_table_on_expired_table_is_the_same_error(make_agent):
    clock = Clock()
    agent, llm, _, name = await run_first(make_agent, clock=clock, result_ttl_ms=HOUR)
    clock.ms += HOUR + 1
    result = await describe_from_other_conversation(agent, llm, name)
    assert result["success"] is False and "no such table" in result["error"].lower()
    assert name not in result["error"]


async def test_expired_table_is_denied_to_other_conversations_without_leaking(make_agent):
    clock = Clock()
    agent, llm, _, name = await run_first(make_agent, clock=clock, result_ttl_ms=HOUR)
    clock.ms += HOUR + 1
    llm.steps = [[tool_call("run_query", sql=f"SELECT * FROM {name}", title="late")], [TextChunk("sorry")]]
    await collect(agent, "use it", Conversation())
    result = last_tool_result(llm.calls[-1])
    assert result["success"] is False and name not in str(result)


async def test_the_querying_conversation_owns_the_new_table_not_the_source(make_agent):
    agent, llm, _, name = await run_first(make_agent)
    other = Conversation()
    llm.steps = [
        [tool_call("run_query", sql=f"SELECT dport FROM {name}", title="derived")],
        [TextChunk("ok")],
    ]
    await collect(agent, "derive", other)
    derived = last_tool_result(llm.calls[-1])["result_table"]
    llm.steps = [[TextChunk("hi")]]
    await collect(agent, "again", other)
    system = llm.calls[-1][0]["content"]
    assert derived in system and f"- {name} " not in system
