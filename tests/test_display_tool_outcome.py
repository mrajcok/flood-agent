"""What the LLM is sent for describe_table, show_table and show_chart outcomes, as exact strings (byte-identical
to before these tools went through the tool outcome codec)."""
import json

from conftest import ScriptedLLM, collect, last_tool_result, tool_call
from test_charts import CHART_SQL, chart_spec

from flood_agent.events import DisplayEvent
from flood_agent.llm import TextChunk, ToolCallChunk


async def run(make_agent, *calls, **agent_kwargs):
    """A run_query, then `calls(table)` (tool calls), then a closing text. Returns (sent strings, events, table)."""
    llm = ScriptedLLM(
        [tool_call("run_query", sql=CHART_SQL, title="t")],
        lambda msgs: [ToolCallChunk(f"c{i}", n, json.dumps(a(last_tool_result(msgs)["result_table"]))) for i, (n, a) in enumerate(calls)],
        [TextChunk("ok")],
    )
    agent = await make_agent(llm, **agent_kwargs)
    events = await collect(agent, "go")
    table = json.loads([m for m in llm.calls[1] if m["role"] == "tool"][-1]["content"])["result_table"]
    sent = [m["content"] for m in llm.calls[2] if m["role"] == "tool"][1:]
    return sent, events, table


async def test_describe_table_found_and_missing_are_sent_as_todays_exact_json(make_agent):
    sent, _, table = await run(make_agent, ("describe_table", lambda t: {"name": t}),
                               ("describe_table", lambda t: {"name": "nope_20200101_000000"}))
    assert sent[0] == (f'{{"success": true, "name": "{table}", "kind": "result", "columns": '
                       '[{"name": "etime", "type": "INT"}, {"name": "total_bytes", "type": "INTEGER"}], "row_count": 3}')
    assert sent[1] == '{"success": false, "error": "No such table."}'


async def test_show_table_ok_and_refused_are_sent_as_todays_exact_json(make_agent):
    sent, events, table = await run(make_agent, ("show_table", lambda t: {"result_table": t}),
                                    ("show_table", lambda t: {"result_table": t, "columns": ["zzz"]}))
    assert sent[0] == (f'{{"success": true, "display": {{"kind": "table", "result_table": "{table}", '
                       f'"columns": ["etime", "total_bytes"]}}}}')
    assert sent[1] == '{"success": false, "error": "Unknown column requested."}'
    assert [(e.kind, e.result_table) for e in events if isinstance(e, DisplayEvent)] == [("table", table)]


async def test_show_chart_ok_note_and_refused_are_sent_as_todays_exact_json(make_agent):
    sent, events, table = await run(make_agent, ("show_chart", lambda t: {"vega_lite_spec": chart_spec(t)}),
                                    ("show_chart", lambda t: {"vega_lite_spec": {"mark": "bar"}}))
    spec = json.dumps(chart_spec(table))
    assert sent[0] == f'{{"success": true, "display": {{"kind": "chart", "result_table": "{table}", "spec": {spec}}}}}'
    assert sent[1].startswith('{"success": false, "error": "Missing data source. ')
    assert [e.kind for e in events if isinstance(e, DisplayEvent)] == ["chart"]


async def test_show_chart_over_the_row_cap_adds_the_note(make_agent):
    sent, _, table = await run(make_agent, ("show_chart", lambda t: {"vega_lite_spec": chart_spec(t)}), chart_row_cap=1)
    spec = json.dumps(chart_spec(table))
    note = ("Only the first 1 rows of the Result Table are charted. Do not present the chart as covering all the data; "
            "to chart everything, aggregate or filter with run_query (e.g. GROUP BY a time bucket) and chart the new "
            "Result Table.")
    assert sent[0] == (f'{{"success": true, "display": {{"kind": "chart", "result_table": "{table}", "spec": {spec}}}, '
                       f'"note": {json.dumps(note)}}}')
