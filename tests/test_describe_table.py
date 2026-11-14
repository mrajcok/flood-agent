"""describe_table and cross-notebook reads of live Result Tables (ADR 0003), through the Agent notebook seam."""

import json

from conftest import ScriptedLLM, all_telemetry_values, collect, last_tool_result, tool_call
from test_chaining_expiry import HOUR, Clock, run_first

from flood_agent.llm import TextChunk, ToolCallChunk

__all__ = ["ScriptedLLM"]


async def describe_from_other_notebook(agent, llm, name):
    llm.steps = [[ToolCallChunk(id="c1", name="describe_table", arguments=json.dumps({"name": name}))], [TextChunk("ok")]]
    await collect(agent, f"describe {name}")
    return last_tool_result(llm.calls[-1])


async def test_describe_table_reports_kind_columns_types_and_row_count_but_no_values(make_agent, telemetry_db):
    agent, llm, _, name = await run_first(make_agent)
    result = await describe_from_other_notebook(agent, llm, name)
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
    result = await describe_from_other_notebook(agent, llm, "no_such_table_20200101_000000")
    assert result["success"] is False
    assert "no such table" in result["error"].lower()
    assert "no_such_table_2020" not in result["error"]


async def test_describe_table_on_expired_table_is_the_same_error(make_agent):
    clock = Clock()
    agent, llm, _, name = await run_first(make_agent, clock=clock, result_ttl_ms=HOUR)
    clock.ms += HOUR + 1
    result = await describe_from_other_notebook(agent, llm, name)
    assert result["success"] is False and "no such table" in result["error"].lower()
    assert name not in result["error"]


async def test_expired_table_is_denied_to_other_notebooks_without_leaking(make_agent):
    clock = Clock()
    agent, llm, _, name = await run_first(make_agent, clock=clock, result_ttl_ms=HOUR)
    clock.ms += HOUR + 1
    llm.steps = [[tool_call("run_query", sql=f"SELECT * FROM {name}", title="late")], [TextChunk("sorry")]]
    await collect(agent, "use it")
    result = last_tool_result(llm.calls[-1])
    assert result["success"] is False and name not in str(result)


async def test_the_querying_notebook_owns_the_new_table_not_the_source(make_agent):
    agent, llm, _, name = await run_first(make_agent)
    other = agent.create_notebook()
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


async def test_describe_table_reports_saved_result_and_lookup_kinds(make_agent):
    agent, llm, _, name = await run_first(make_agent)
    agent.tables.add_lookup("hosts", "ip\n10.0.0.1")
    assert (await describe_from_other_notebook(agent, llm, name))["kind"] == "result"
    assert (await describe_from_other_notebook(agent, llm, "lookup_hosts"))["kind"] == "lookup"
    agent.tables.update(name, "busy_ports")
    assert (await describe_from_other_notebook(agent, llm, "BUSY_ports"))["kind"] == "saved"


async def test_show_table_and_show_chart_on_a_lookup_table_say_it_cannot_be_shown(make_agent):
    agent, llm, _, _ = await run_first(make_agent)
    agent.tables.add_lookup("hosts", "ip\n10.0.0.1")
    chart = {"data": {"name": "lookup_hosts"}, "mark": "bar", "encoding": {}}
    llm.steps = [
        [tool_call("show_table", "c1", result_table="lookup_hosts"), tool_call("show_chart", "c2", vega_lite_spec=chart)],
        [TextChunk("ok")],
    ]
    await collect(agent, "show it")
    tool_results = [json.loads(m["content"]) for m in llm.calls[-1] if m["role"] == "tool"]
    assert len(tool_results) == 2
    assert all("Lookup Table" in r["error"] and "cannot be shown" in r["error"] for r in tool_results)


async def test_a_table_whose_retention_ends_mid_turn_is_refused_by_the_next_query_and_show(make_agent):
    clock = Clock()
    agent, llm, _, name = await run_first(make_agent, clock=clock, result_ttl_ms=HOUR)

    def expire_then_query(_messages):
        clock.ms += HOUR + 1
        return [tool_call("run_query", "c2", sql=f"SELECT * FROM {name}", title="late")]

    llm.steps = [
        [ToolCallChunk(id="c1", name="describe_table", arguments=json.dumps({"name": name}))],
        expire_then_query,
        [tool_call("show_table", "c3", result_table=name)],
        [TextChunk("ok")],
    ]
    await collect(agent, "use it")
    results = [json.loads(m["content"]) for m in llm.calls[-1] if m["role"] == "tool"]
    assert [r["success"] for r in results] == [True, False, False]
    assert all(name not in json.dumps(r) for r in results[1:])


async def test_a_renamed_table_is_shown_charted_and_described_as_lower_case_whatever_the_typed_case(make_agent):
    agent, llm, _, name = await run_first(make_agent)
    assert agent.tables.update(name, "MyTable").name == "mytable"
    chart = {"data": {"name": "MyTable"}, "mark": "bar", "encoding": {
        "x": {"field": "dport", "type": "nominal"}, "y": {"field": "total_bytes", "type": "quantitative"}}}
    llm.steps = [
        [tool_call("show_table", "c1", result_table="MyTable"), tool_call("show_chart", "c2", vega_lite_spec=chart),
         ToolCallChunk(id="c3", name="describe_table", arguments=json.dumps({"name": "MYTABLE"}))],
        [TextChunk("ok")],
    ]
    await collect(agent, "show it")
    results = [json.loads(m["content"]) for m in llm.calls[-1] if m["role"] == "tool"]
    assert all(r["success"] for r in results)
    assert results[0]["display"]["result_table"] == "mytable" == results[1]["display"]["result_table"]
    assert results[1]["display"]["spec"]["data"] == {"name": "mytable"}
    assert results[2]["name"] == "mytable"
