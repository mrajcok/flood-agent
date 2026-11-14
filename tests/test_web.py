"""Thin smoke test of the HTTP/SSE adapter over the Agent's notebook cell stream."""

import pytest
from conftest import ScriptedLLM, ask, last_tool_result, mcp_children, tool_call
from fastapi.testclient import TestClient

from flood_agent.llm import TextChunk
from flood_agent.web import create_app


def test_notebook_page_stream_and_row_endpoint(build_agent, web_client):
    llm = ScriptedLLM(
        [tool_call("run_query", sql="SELECT dport, bytes FROM flows ORDER BY bytes DESC", title="t")],
        lambda msgs: [tool_call("show_table", result_table=last_tool_result(msgs)["result_table"])],
        [TextChunk("Here you go.")],
    )
    client = web_client(build_agent(llm))

    page = client.get("/")
    assert page.status_code == 200 and "htmx" in page.text and "alpinejs" in page.text

    nb, cell, body = ask(client, "show bytes by port")
    assert "show bytes by port" in client.get(f"/n/{nb}").text
    assert "event: text" in body and "Here you go." in body
    assert "event: display" in body and "event: end" in body
    table = body.split('data-rows-url="')[1].split('"')[0]

    rows = client.get(table).json()
    assert rows["columns"] == ["dport", "bytes"]
    assert rows["rows"][0] == [8443, 987_654]


def test_row_endpoint_rejects_unknown_or_malformed_names(build_agent, web_client):
    client = web_client(build_agent(ScriptedLLM()))
    assert client.get("/api/results/nope/rows").status_code == 404
    assert client.get("/api/results/rt_deadbeef/rows").status_code == 404


@pytest.mark.subprocess
def test_entering_the_client_starts_one_mcp_server_and_leaving_it_stops_it(build_agent):
    llm = ScriptedLLM([TextChunk("Hello.")])
    assert mcp_children() == []
    with TestClient(create_app(build_agent(llm))) as client:
        assert len(mcp_children()) == 1
        _, _, body = ask(client, "hi")
        assert "Hello." in body and "event: end" in body
        assert len(mcp_children()) == 1
    assert mcp_children() == []


@pytest.mark.subprocess
def test_app_startup_fails_when_the_mcp_server_cannot_start(build_agent, monkeypatch):
    agent = build_agent(ScriptedLLM())
    monkeypatch.setattr("flood_agent.agent.sys.executable", "/nonexistent/python")
    with pytest.raises(Exception):
        with TestClient(create_app(agent)):
            pass


def _rows_url_after_showing(client, max_rows):
    _, _, body = ask(client, "show", max_rows=max_rows)
    return body.split('data-rows-url="')[1].split('"')[0]


def test_row_endpoint_reports_the_cap_a_truncated_table_was_cut_to(build_agent, web_client):
    def script():
        return ScriptedLLM(
            [tool_call("run_query", sql="SELECT dport FROM flows", title="t")],
            lambda msgs: [tool_call("show_table", result_table=last_tool_result(msgs)["result_table"])],
            [TextChunk("done")],
        )

    client = web_client(build_agent(script()))
    truncated = client.get(_rows_url_after_showing(client, "2")).json()
    assert truncated["total"] == 2 and truncated["truncated_to"] == 2

    client = web_client(build_agent(script()))
    complete = client.get(_rows_url_after_showing(client, "3000")).json()
    assert complete["total"] == 3 and complete["truncated_to"] is None


def test_table_display_has_a_truncation_badge_in_the_pager_line(build_agent, web_client):
    client = web_client(build_agent(ScriptedLLM()))
    markup = client.get("/").text
    assert "badge-truncated" in markup  # styled on the page
    from flood_agent.web import TEMPLATES

    template = TEMPLATES.get_template("display_table.html").render(rows_url="r", psv_url="p", result_table="t")
    pager = template.split('<div class="pager">')[1].split("</div>")[0]
    assert "truncated to" in pager and "Download PSV" in pager


def test_the_app_is_named_canopy(build_agent):
    with TestClient(create_app(build_agent(ScriptedLLM([TextChunk("Hi.")])))) as client:
        assert "<h1>CANOPY</h1>" in client.get("/").text
        assert client.get("/openapi.json").json()["info"]["title"] == "CANOPY"
