"""\"No model\": a per-prompt Model Choice answered by the scripted stand-in (Fake Mode), with no key needed."""

import re

from conftest import ScriptedLLM
from fastapi.testclient import TestClient

from flood_agent.__main__ import build_app, picker_entries
from flood_agent.agent import NO_MODEL
from flood_agent.config import Settings
from flood_agent.fake_llm import FakeClient, load_canned_prompts
from flood_agent.llm import TextChunk

SONNET, GLM = "anthropic/claude-sonnet-5.5", "z-ai/glm-5.3"
CANNED = load_canned_prompts()


def post_chat(client, **data):
    return client.post("/chat", data={"message": "hello", "max_rows": "1000", **data})


def finish_turn(client, posted):
    return client.get(posted.text.split('sse-connect="')[1].split('"')[0]).text


def conversation_id(posted):
    return re.search(r'name="conversation_id" value="([0-9a-f]+)"', posted.text).group(1)


def meter_text(html):
    inner = re.search(r'<span id="context-meter"[^>]*>(.*?)</span>(?:</span>)?', html, re.S).group(1)
    return re.sub(r"<[^>]*>", "", inner).strip()


def fake_or_real(real):
    """A client factory: the scripted stand-in for No model, `real` for every real model."""
    fake = FakeClient(CANNED, chunk_delay_s=0)
    return lambda model: fake if model == NO_MODEL else real


def test_no_model_answers_with_the_scripted_stand_in_and_the_trace_names_it(build_agent, web_client):
    real = ScriptedLLM()
    agent = build_agent(real)
    agent.client_for = fake_or_real(real)
    client = web_client(agent)

    body = finish_turn(client, post_chat(client, model=NO_MODEL))

    assert "no language model is involved" in body and "No model" in body
    assert real.calls == []


def test_a_blank_model_field_means_the_default_model(build_agent, web_client):
    client = web_client(build_agent(ScriptedLLM([TextChunk("hi")])))

    assert "glm-5.3" in finish_turn(client, post_chat(client, model=""))


def test_the_meter_ignores_fake_turns(build_agent, web_client):
    real = ScriptedLLM([TextChunk("hi")])
    agent = build_agent(real)
    agent.client_for = fake_or_real(real)
    client = web_client(agent)
    posted = post_chat(client, model=NO_MODEL)
    cid = conversation_id(posted)
    fake_body = finish_turn(client, posted)

    def meter_for(model):
        return meter_text(client.get("/context-meter", params={"model": model, "conversation_id": cid}).text)

    assert 'id="context-meter"' not in fake_body  # a fake turn leaves the meter alone
    assert meter_for(GLM) == "0 of 1.05M"
    assert meter_for(NO_MODEL) == "no context used"

    finish_turn(client, post_chat(client, model=SONNET, conversation_id=cid))
    finish_turn(client, post_chat(client, model=NO_MODEL, conversation_id=cid))  # a later fake turn changes nothing
    assert meter_for(GLM) == "100 of 1.05M"


def test_a_reopened_conversation_meter_skips_its_fake_turns(build_agent, web_client):
    real = ScriptedLLM()
    agent = build_agent(real)
    agent.client_for = fake_or_real(real)
    client = web_client(agent)
    posted = post_chat(client, model=NO_MODEL)
    finish_turn(client, posted)

    assert meter_text(client.get(f"/c/{conversation_id(posted)}").text) == "0 of 1.05M"


def test_no_model_is_listed_last_and_is_always_selectable(build_agent, web_client):
    page = web_client(build_agent(ScriptedLLM()), has_api_key=False).get("/").text

    entries = re.findall(r"\{[^{}]*?label[^{}]*?\}", page)
    assert "No model" in entries[-1] and re.search(r"disabled(?:\"|&#34;):\s*false", entries[-1])
    assert all(re.search(r"disabled(?:\"|&#34;):\s*true", e) for e in entries[:-1])


def test_without_a_key_the_page_preselects_no_model(build_agent, web_client):
    page = web_client(build_agent(ScriptedLLM()), has_api_key=False).get("/").text

    assert 'id="model" name="model" :value="model" value="no-model"' in page
    assert meter_text(page) == "no context used"


def test_with_a_key_the_page_still_preselects_the_default_model(build_agent, web_client):
    page = web_client(build_agent(ScriptedLLM())).get("/").text

    assert f'id="model" name="model" :value="model" value="{GLM}"' in page


def test_no_badge_and_the_canned_prompt_list_is_on_the_page(build_agent, web_client):
    page = web_client(build_agent(ScriptedLLM()), picker=picker_entries(CANNED)).get("/").text

    assert "Fake Mode" not in page and 'class="badge"' not in page
    assert CANNED[0].prompt in page


def test_the_real_client_factory_gives_the_stand_in_for_no_model_without_a_key():
    factory = Settings().client_factory()  # pyright: ignore[reportCallIssue]

    assert isinstance(factory(NO_MODEL), FakeClient) and factory(NO_MODEL) is factory(NO_MODEL)


def test_the_app_starts_without_a_key_and_answers_no_model_prompts(tmp_path, telemetry_db, semantic_layer_path):
    settings = Settings(  # pyright: ignore[reportCallIssue]
        telemetry_db=telemetry_db, results_db=tmp_path / "results.db", app_db=tmp_path / "app.db",
        semantic_layer=semantic_layer_path,
    )

    with TestClient(build_app(settings)) as client:
        assert 'value="no-model"' in client.get("/").text
        body = finish_turn(client, post_chat(client, model=NO_MODEL))
        refused = post_chat(client, model=SONNET)

    assert "no language model is involved" in body
    assert refused.status_code == 422 and "OPENROUTER_API_KEY" in refused.json()["detail"]
