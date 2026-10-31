"""Thin smoke test of the HTTP/SSE adapter over the Agent turn stream."""

from fastapi.testclient import TestClient

from flood_defender.llm import TextChunk, DEFAULT_MODEL, model_from_env
from flood_defender.web import create_app

from conftest import ScriptedLLM, last_tool_result, tool_call


def test_chat_page_stream_and_row_endpoint(make_agent):
    llm = ScriptedLLM(
        [tool_call("run_query", sql="SELECT dport, bytes FROM flows ORDER BY bytes DESC", title="t")],
        lambda msgs: [tool_call("show_table", result_table=last_tool_result(msgs)["result_table"])],
        [TextChunk("Here you go.")],
    )
    client = TestClient(create_app(make_agent(llm)))

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


def test_row_endpoint_rejects_unknown_or_malformed_names(make_agent):
    client = TestClient(create_app(make_agent(ScriptedLLM())))
    assert client.get("/api/results/nope/rows").status_code == 404
    assert client.get("/api/results/rt_deadbeef/rows").status_code == 404


def test_model_defaults_and_env_override(monkeypatch):
    monkeypatch.delenv("OPENROUTER_MODEL", raising=False)
    assert model_from_env() == DEFAULT_MODEL == "anthropic/claude-sonnet-5.5"
    monkeypatch.setenv("OPENROUTER_MODEL", "z-ai/glm-5.3")
    assert model_from_env() == "z-ai/glm-5.3"
