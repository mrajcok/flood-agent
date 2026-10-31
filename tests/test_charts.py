"""Chart tests at the Agent turn seam: MCP server in-process, scripted fake LLM."""

import pytest
from conftest import ScriptedLLM, all_telemetry_values, collect, last_tool_result, tool_call

from flood_defender.agent import DisplayEvent
from flood_defender.llm import TextChunk

CHART_SQL = "SELECT etime, SUM(bytes) AS total_bytes FROM flows GROUP BY etime"
TOP_PORTS_SQL = "SELECT dport, SUM(bytes) AS total_bytes FROM flows GROUP BY dport ORDER BY total_bytes DESC"
HEATMAP_SQL = "SELECT sip, dport, SUM(bytes) AS b FROM flows GROUP BY sip, dport"


def chart_spec(table, mark="line", encoding=None):
    return {
        "data": {"name": table},
        "mark": mark,
        "encoding": encoding
        or {
            "x": {"field": "etime", "type": "temporal"},
            "y": {"field": "total_bytes", "type": "quantitative"},
        },
    }


def chart_script(spec_for, sql=CHART_SQL):
    """run_query, then show_chart with spec_for(table), then a closing text."""
    return ScriptedLLM(
        [tool_call("run_query", sql=sql, title="t")],
        lambda msgs: [
            tool_call("show_chart", vega_lite_spec=spec_for(last_tool_result(msgs)["result_table"]))
        ],
        [TextChunk("chart done")],
    )


def displays(events):
    return [e for e in events if isinstance(e, DisplayEvent)]


@pytest.mark.parametrize(
    "mark,sql,encoding",
    [
        ("line", CHART_SQL, None),
        (
            "bar",
            TOP_PORTS_SQL,
            {"x": {"field": "dport", "type": "nominal"}, "y": {"field": "total_bytes", "type": "quantitative"}},
        ),
        (
            "rect",
            HEATMAP_SQL,
            {
                "x": {"field": "dport", "type": "nominal"},
                "y": {"field": "sip", "type": "nominal"},
                "color": {"field": "b", "type": "quantitative"},
            },
        ),
    ],
)
async def test_valid_chart_spec_yields_chart_display_pointing_at_row_endpoint(make_agent, mark, sql, encoding):
    agent = await make_agent(chart_script(lambda t: chart_spec(t, mark, encoding), sql))
    ds = displays(await collect(agent, "chart it"))
    assert len(ds) == 1 and ds[0].kind == "chart"
    d = ds[0]
    assert d.spec["data"] == {"url": f"/api/results/{d.result_table}/chart-rows"}
    assert d.spec["mark"] == mark
    rows = agent.fetch_chart_rows(d.result_table)
    assert len(rows) >= 2 and isinstance(rows[0], dict)


async def test_pre_and_post_resolution_specs_are_both_retained(make_agent):
    d = displays(await collect(await make_agent(chart_script(chart_spec)), "chart it"))[0]
    assert d.source_spec["data"] == {"name": d.result_table}
    assert d.spec["data"] == {"url": f"/api/results/{d.result_table}/chart-rows"}


@pytest.mark.parametrize(
    "mutate",
    [
        lambda s: s.update(data={"values": [{"etime": 1, "total_bytes": 2}]}),
        lambda s: s.update(data={"url": "http://evil.example/x.json"}),
        lambda s: s.update(datasets={"d": [{"a": 1}]}),
        lambda s: s.update(data={"name": "flows"}),
        lambda s: s.update(data={"name": "rt_00000000"}),
        lambda s: s.pop("data"),
        lambda s: s.update(layer=[{"mark": "point", "data": {"values": [{"a": 1}]}}]),
    ],
)
async def test_inline_or_foreign_data_is_rejected_with_guidance(make_agent, mutate):
    def spec_for(t):
        s = chart_spec(t)
        mutate(s)
        return s

    llm = chart_script(spec_for)
    events = await collect(await make_agent(llm), "chart it")
    assert not displays(events)
    result = last_tool_result(llm.calls[2])
    assert result["success"] is False
    assert '{"name"' in result["error"]


async def test_chart_over_row_cap_is_refused_with_guidance_to_aggregate(make_agent):
    llm = chart_script(chart_spec)
    agent = await make_agent(llm, chart_row_cap=1)  # the server's launch settings come from the config
    events = await collect(agent, "chart it")
    assert not displays(events)
    result = last_tool_result(llm.calls[2])
    assert result["success"] is False and "aggregate" in result["error"].lower()


async def test_table_and_chart_displays_come_in_order(make_agent):
    llm = ScriptedLLM(
        [tool_call("run_query", sql=CHART_SQL, title="t")],
        lambda msgs: [
            tool_call("show_table", call_id="a", result_table=last_tool_result(msgs)["result_table"]),
            tool_call("show_chart", call_id="b", vega_lite_spec=chart_spec(last_tool_result(msgs)["result_table"])),
        ],
        [TextChunk("both")],
    )
    events = await collect(await make_agent(llm), "both")
    assert [e.kind for e in displays(events)] == ["table", "chart"]
    assert sorted(llm.tools_seen_names) == ["run_query", "show_chart", "show_table"]


async def test_no_telemetry_value_reaches_the_llm_with_charts(make_agent, telemetry_db):
    llm = chart_script(chart_spec, "SELECT sip, etime, SUM(bytes) AS total_bytes FROM flows GROUP BY sip, etime")
    await collect(await make_agent(llm), "chart")
    values = {v for v in all_telemetry_values(telemetry_db) if len(v) >= 4}
    for call in llm.calls:
        for m in call:
            if m["role"] in ("system", "tool"):
                for v in values:
                    assert v not in m["content"], v
