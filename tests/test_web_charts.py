"""Thin smoke test of chart delivery through the HTTP/SSE adapter."""

import json

from fastapi.testclient import TestClient

from flood_defender.llm import TextChunk
from flood_defender.web import create_app

from conftest import ScriptedLLM, last_tool_result, tool_call


def test_chart_display_is_streamed_and_rows_served_from_chart_endpoint(make_agent):
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
    client = TestClient(create_app(make_agent(llm)))
    posted = client.post("/chat", data={"message": "chart bytes"})
    stream_url = posted.text.split('sse-connect="')[1].split('"')[0]
    body = client.get(stream_url).text

    assert "event: display" in body and "vegaEmbed" in body
    chart_url = body.split("/api/results/")[1].split("/chart-rows")[0]
    rows = client.get(f"/api/results/{chart_url}/chart-rows").json()
    assert rows[0] == {"dport": 8443, "bytes": 987_654}


def test_page_pins_vega_cdn_versions_and_component_finalizes_view(make_agent):
    import re

    client = TestClient(create_app(make_agent(ScriptedLLM())))
    page = client.get("/").text
    for lib in ("vega", "vega-lite", "vega-embed"):
        assert re.search(rf"/{lib}@\d+\.\d+\.\d+\"", page), lib
    from pathlib import Path

    tpl = (Path(__file__).parent.parent / "src/flood_defender/templates/display_chart.html").read_text()
    assert "finalize()" in tpl and "destroy()" in tpl


def test_chart_rows_endpoint_rejects_unknown_tables(make_agent):
    client = TestClient(create_app(make_agent(ScriptedLLM())))
    assert client.get("/api/results/nope/chart-rows").status_code == 404


def test_reopening_rerenders_chart_display(make_agent):
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
    client = TestClient(create_app(make_agent(llm)))
    posted = client.post("/chat", data={"message": "chart bytes"})
    import re

    cid = re.search(r'name="conversation_id" value="([0-9a-f]+)"', posted.text).group(1)
    client.get(posted.text.split('sse-connect="')[1].split('"')[0])

    restarted = TestClient(create_app(make_agent(ScriptedLLM())))
    page = restarted.get(f"/c/{cid}").text
    assert "vegaEmbed" in page and "/chart-rows" in page
    import html

    url = html.unescape(page.split('data-spec="')[1].split('"')[0])
    url = json.loads(url)["data"]["url"]
    assert restarted.get(url).json()[0] == {"dport": 8443, "bytes": 987_654}
