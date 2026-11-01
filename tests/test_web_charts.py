"""Thin smoke test of chart delivery through the HTTP/SSE adapter."""

import json

from conftest import ScriptedLLM, last_tool_result, tool_call

from flood_defender.llm import TextChunk


def test_chart_display_is_streamed_and_rows_served_from_chart_endpoint(build_agent, web_client):
    def spec(msgs):
        t = last_tool_result(msgs)["result_table"]
        return [
            tool_call(
                "show_chart",
                vega_lite_spec={
                    "data": {"name": t},
                    "mark": "bar",
                    "encoding": {
                        "x": {"field": "dport", "type": "nominal"},
                        "y": {"field": "bytes", "type": "quantitative"},
                    },
                },
            )
        ]

    llm = ScriptedLLM(
        [tool_call("run_query", sql="SELECT dport, bytes FROM flows ORDER BY bytes DESC", title="t")],
        spec,
        [TextChunk("chart")],
    )
    client = web_client(build_agent(llm))
    posted = client.post("/chat", data={"message": "chart bytes"})
    stream_url = posted.text.split('sse-connect="')[1].split('"')[0]
    body = client.get(stream_url).text

    assert "event: display" in body and "vegaEmbed" in body
    chart_url = body.split("/api/results/")[1].split("/chart-rows")[0]
    rows = client.get(f"/api/results/{chart_url}/chart-rows").json()
    assert rows[0] == {"dport": 8443, "bytes": 987_654}


def test_page_pins_vega_cdn_versions_and_component_finalizes_view(build_agent, web_client):
    import re

    client = web_client(build_agent(ScriptedLLM()))
    page = client.get("/").text
    for lib in ("vega", "vega-lite", "vega-embed"):
        assert re.search(rf"/{lib}@\d+\.\d+\.\d+\"", page), lib
    from pathlib import Path

    tpl = (Path(__file__).parent.parent / "src/flood_defender/templates/display_chart.html").read_text()
    assert "finalize()" in tpl and "destroy()" in tpl


def test_chart_rows_endpoint_rejects_unknown_tables(build_agent, web_client):
    client = web_client(build_agent(ScriptedLLM()))
    assert client.get("/api/results/nope/chart-rows").status_code == 404


def test_reopening_rerenders_chart_display(build_agent, web_client):
    def spec(msgs):
        t = last_tool_result(msgs)["result_table"]
        return [
            tool_call(
                "show_chart",
                vega_lite_spec={
                    "data": {"name": t},
                    "mark": "bar",
                    "encoding": {
                        "x": {"field": "dport", "type": "nominal"},
                        "y": {"field": "bytes", "type": "quantitative"},
                    },
                },
            )
        ]

    llm = ScriptedLLM(
        [tool_call("run_query", sql="SELECT dport, bytes FROM flows ORDER BY bytes DESC", title="t")],
        spec,
        [TextChunk("chart")],
    )
    client = web_client(build_agent(llm))
    posted = client.post("/chat", data={"message": "chart bytes"})
    import re

    cid = re.search(r'name="conversation_id" value="([0-9a-f]+)"', posted.text).group(1)
    client.get(posted.text.split('sse-connect="')[1].split('"')[0])

    restarted = web_client(build_agent(ScriptedLLM()))
    page = restarted.get(f"/c/{cid}").text
    assert "vegaEmbed" in page and "/chart-rows" in page
    import html

    url = html.unescape(page.split('data-spec="')[1].split('"')[0])
    url = json.loads(url)["data"]["url"]
    assert restarted.get(url).json()[0] == {"dport": 8443, "bytes": 987_654}


def test_main_pane_fills_the_space_beside_the_sidebar_so_charts_are_as_wide_as_the_trace(build_agent, web_client):
    from conftest import ScriptedLLM

    page = web_client(build_agent(ScriptedLLM())).get("/").text
    assert "main { flex: 1; min-width: 0;" in page  # otherwise main shrink-wraps its content
