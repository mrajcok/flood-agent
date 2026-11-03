"""Renaming a Result Table is a real SQL rename, at the HTTP seam."""

import re

import pytest
from conftest import ScriptedLLM, last_tool_result, tool_call

from flood_agent.llm import TextChunk
from flood_agent.web import TEMPLATES

BY_PORT = "SELECT dport, SUM(bytes) AS total_bytes FROM flows GROUP BY dport"


def chart_spec(t):
    return {"data": {"name": t}, "mark": "bar", "encoding": {
        "x": {"field": "dport", "type": "nominal"}, "y": {"field": "total_bytes", "type": "quantitative"}}}


def make(build_agent, web_client, *followups):
    """One turn that makes a Result Table, shows it and charts it; then any scripted follow-up turns."""
    seen = {}

    def table(msgs):
        seen["t"] = last_tool_result(msgs)["result_table"]
        return [tool_call("show_table", result_table=seen["t"])]

    def chart(msgs):
        return [tool_call("show_chart", vega_lite_spec=chart_spec(seen["t"]))]

    llm = ScriptedLLM(
        [tool_call("run_query", sql=BY_PORT, title="Bytes per port")],
        table, chart, [TextChunk("here")], *followups)
    client = web_client(build_agent(llm))
    cid, _ = chat(client, "ports")
    return client, cid, seen["t"]


def chat(client, message, cid=""):
    posted = client.post("/chat", data={"message": message, "conversation_id": cid, "max_rows": "1000"})
    cid = re.search(r'name="conversation_id" value="([0-9a-f]+)"', posted.text).group(1)
    return cid, client.get(posted.text.split('sse-connect="')[1].split('"')[0]).text


def rename(client, old, new):
    return client.post(f"/api/results/{old}/settings", data={"title": new})


def test_rename_changes_the_name_everywhere_and_keeps_displays_loading(build_agent, web_client):
    client, cid, old = make(build_agent, web_client)
    resp = rename(client, old, "  top_ports ")
    assert resp.status_code == 200 and resp.json()["title"] == "top_ports"
    assert client.get("/api/results/top_ports/rows").json()["title"] == "top_ports"
    assert client.get(f"/api/results/{old}/rows").status_code == 404
    assert client.get("/api/results/top_ports/chart-rows").json()[0]["dport"] == 8443
    assert client.get("/api/results/top_ports/psv").status_code == 200
    page = client.get(f"/c/{cid}").text
    assert "/api/results/top_ports/rows" in page and "/api/results/top_ports/chart-rows" in page


def test_new_name_is_queryable_and_old_is_not(build_agent, web_client):
    results, seen = [], {}

    def count(table):
        def step(msgs):
            if table is None:  # the second query follows the first one's result
                results.append(last_tool_result(msgs))
            return [tool_call("run_count_query", sql=f"SELECT COUNT(*) FROM {seen.get('old') if table is None else table}")]
        return step

    def done(msgs):
        results.append(last_tool_result(msgs))
        return [TextChunk("done")]

    client, cid, seen["old"] = make(build_agent, web_client, count("top_ports"), count(None), done)
    assert rename(client, seen["old"], "top_ports").status_code == 200
    chat(client, "again", cid)
    assert results[-2]["success"] is True and results[-2]["count"] > 0
    assert results[-1]["success"] is False


@pytest.mark.parametrize("bad", ["1abc", "has space", "a-b", "x" * 61, "rt_abc", "lookup_ips", "drop;table", "é"])
def test_invalid_or_taken_names_are_rejected_and_nothing_changes(build_agent, web_client, bad):
    client, _, old = make(build_agent, web_client)
    resp = rename(client, old, bad)
    assert resp.status_code == 422 and resp.json()["detail"]
    assert client.get(f"/api/results/{old}/rows").status_code == 200


def test_renaming_to_another_result_table_is_rejected(build_agent, web_client):
    client, cid, old = make(build_agent, web_client)
    assert rename(client, old, old).status_code == 422
    assert client.get(f"/api/results/{old}/rows").json()["title"] == old


def test_retention_survives_a_rename(build_agent, web_client):
    client, _, old = make(build_agent, web_client)
    client.post(f"/api/results/{old}/settings", data={"retention_days": "7"})
    assert rename(client, old, "kept").json()["retention_days"] == 7
    assert client.get("/api/results/kept/rows").json()["retention_days"] == 7


def test_deleting_the_conversation_drops_the_renamed_table(build_agent, web_client):
    client, cid, old = make(build_agent, web_client)
    rename(client, old, "kept")
    client.post(f"/c/{cid}/delete", data={"tables": "delete_all"})
    assert client.get("/api/results/kept/rows").status_code == 404


def test_display_shows_the_sql_name_with_a_copy_button():
    html = TEMPLATES.get_template("display_table.html").render(
        rows_url="/r", psv_url="/p", result_table="t", columns=[], settings_url="/s")
    assert "clipboard" in html and "Copy" in html
