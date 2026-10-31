"""Thin smoke test of the HTTP/SSE adapter over the Agent turn stream."""

import pytest
from fastapi.testclient import TestClient

from flood_defender.llm import TextChunk
from flood_defender.web import create_app

from conftest import ScriptedLLM, last_tool_result, mcp_children, tool_call


def test_chat_page_stream_and_row_endpoint(build_agent, web_client):
    llm = ScriptedLLM(
        [tool_call("run_query", sql="SELECT dport, bytes FROM flows ORDER BY bytes DESC", title="t")],
        lambda msgs: [tool_call("show_table", result_table=last_tool_result(msgs)["result_table"])],
        [TextChunk("Here you go.")],
    )
    client = web_client(build_agent(llm))

    page = client.get("/")
    assert page.status_code == 200 and "htmx" in page.text and "alpinejs" in page.text

    posted = client.post("/chat", data={"message": "show bytes by port"})
    assert posted.status_code == 200 and "show bytes by port" in posted.text
    stream_url = posted.text.split('sse-connect="')[1].split('"')[0]

    body = client.get(stream_url).text
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


def test_reopening_a_conversation_sweeps_expired_tables_once_not_per_display(build_agent, web_client):
    def show(msgs):
        return [tool_call("show_table", result_table=last_tool_result(msgs)["result_table"])]

    steps = []
    for i in range(3):  # three table Displays in one Conversation
        steps += [[tool_call("run_query", f"q{i}", sql="SELECT dport, bytes FROM flows", title="t")], show]
    agent = build_agent(ScriptedLLM(*steps, [TextChunk("done")]))
    client = web_client(agent)
    posted = client.post("/chat", data={"message": "show"})
    client.get(posted.text.split('sse-connect="')[1].split('"')[0])
    cid = agent.store.list_conversations()[0]["id"]

    sweeps = []
    real = agent.sweep_expired
    agent.sweep_expired = lambda: (sweeps.append(1), real())[1]
    page = client.get(f"/c/{cid}").text
    assert page.count("data-rows-url=") == 3
    assert len(sweeps) == 1


def test_entering_the_client_starts_one_mcp_server_and_leaving_it_stops_it(build_agent):
    llm = ScriptedLLM([TextChunk("Hello.")])
    assert mcp_children() == []
    with TestClient(create_app(build_agent(llm))) as client:
        assert len(mcp_children()) == 1
        posted = client.post("/chat", data={"message": "hi"})
        body = client.get(posted.text.split('sse-connect="')[1].split('"')[0]).text
        assert "Hello." in body and "event: end" in body
        assert len(mcp_children()) == 1
    assert mcp_children() == []


def test_app_startup_fails_when_the_mcp_server_cannot_start(build_agent, monkeypatch):
    agent = build_agent(ScriptedLLM())
    monkeypatch.setattr("flood_defender.agent.sys.executable", "/nonexistent/python")
    with pytest.raises(Exception):
        with TestClient(create_app(agent)):
            pass
