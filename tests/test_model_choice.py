"""Choosing a model from the curated list: it travels with the prompt, is validated, picks the client,
shows in the Trace and sets the meter's window."""

import re

import pytest
from conftest import ScriptedLLM, collect

from flood_agent.agent import Conversation, TraceReady
from flood_agent.config import Settings
from flood_agent.llm import OpenRouterClient, TextChunk

SONNET, GLM = "anthropic/claude-sonnet-5.5", "z-ai/glm-5.3"


def post_chat(client, **data):
    return client.post("/chat", data={"message": "hello", "max_rows": "1000", **data})


def finish_turn(client, posted):
    return client.get(posted.text.split('sse-connect="')[1].split('"')[0]).text


class RecordingFactory:
    """A client factory that answers every model with one scripted fake and remembers who asked."""

    def __init__(self, llm):
        self.llm, self.asked = llm, []

    def __call__(self, model):
        self.asked.append(model)
        return self.llm


async def collect_with_model(agent, message, conversation, model):
    return [e async for e in agent.run_turn(conversation, message, 1_000_000, model)]


def trace_of(events):
    return next(e.trace for e in events if isinstance(e, TraceReady))


async def test_a_turn_runs_the_client_of_the_model_chosen_for_it(make_agent):
    llm = ScriptedLLM([TextChunk("a")], [TextChunk("b")])
    agent = await make_agent(llm)
    factory = RecordingFactory(llm)
    agent.client_for = factory
    conversation = Conversation()

    first = await collect_with_model(agent, "q1", conversation, SONNET)
    second = await collect_with_model(agent, "q2", conversation, GLM)

    assert factory.asked == [SONNET, GLM]
    assert [trace_of(first)["model"], trace_of(second)["model"]] == [SONNET, GLM]  # one Conversation, two models


async def test_a_turn_without_a_model_choice_runs_the_default_model(make_agent):
    agent = await make_agent(ScriptedLLM([TextChunk("a")]))

    assert trace_of(await collect(agent, "q"))["model"] == GLM


def test_the_chosen_model_is_what_the_stream_runs_and_the_trace_shows(build_agent, web_client):
    client = web_client(build_agent(ScriptedLLM([TextChunk("hi")])))

    assert SONNET in finish_turn(client, post_chat(client, model=SONNET))


def test_a_missing_model_field_means_the_default_model(build_agent, web_client):
    client = web_client(build_agent(ScriptedLLM([TextChunk("hi")])))

    assert GLM in finish_turn(client, post_chat(client))


@pytest.mark.parametrize("bad", ["nobody/nothing", "No model"])
def test_an_unknown_model_id_is_rejected_before_any_turn_starts(build_agent, web_client, bad):
    llm = ScriptedLLM([TextChunk("never")])
    agent = build_agent(llm)
    client = web_client(agent)

    response = post_chat(client, model=bad)

    assert response.status_code == 422 and "unknown model" in response.json()["detail"]
    assert llm.calls == [] and agent.store.list_conversations() == []


def test_with_no_key_a_real_model_is_refused_with_a_clear_message_and_never_faked(build_agent, web_client):
    llm = ScriptedLLM([TextChunk("never")])
    client = web_client(build_agent(llm), has_api_key=False)

    response = post_chat(client, model=SONNET)

    assert response.status_code == 422 and "OPENROUTER_API_KEY" in response.json()["detail"]
    assert llm.calls == []


def test_with_no_key_every_real_model_shows_as_disabled(build_agent, web_client):
    page = web_client(build_agent(ScriptedLLM()), has_api_key=False).get("/").text

    assert "needs OPENROUTER_API_KEY" in page
    assert re.search(r'disabled(?:"|&#34;):\s*true', page) and len(re.findall(r"disabled(?:\"|&#34;):\s*false", page)) == 1  # only No model is enabled


def test_with_a_key_the_default_model_is_selectable(build_agent, web_client):
    page = web_client(build_agent(ScriptedLLM())).get("/").text

    assert re.search(r'disabled(?:"|&#34;):\s*false', page) and not re.search(r'disabled(?:"|&#34;):\s*true', page)
    assert GLM in page


def meter_text(html):
    return re.search(r'<span id="context-meter"[^>]*>(.*?)</span>', html, re.S).group(1)


def test_the_meter_uses_the_window_of_the_model_selected_for_the_next_prompt(build_agent, web_client):
    client = web_client(build_agent(ScriptedLLM([TextChunk("hi")])))
    posted = post_chat(client, model=GLM)
    cid = re.search(r'name="conversation_id" value="([0-9a-f]+)"', posted.text).group(1)
    finish_turn(client, posted)

    glm = client.get("/context-meter", params={"model": GLM, "conversation_id": cid}).text
    sonnet = client.get("/context-meter", params={"model": SONNET, "conversation_id": cid}).text

    assert "100 of 1.05M" in meter_text(glm) and "100 of 1M" in meter_text(sonnet)
    assert client.get("/context-meter", params={"model": "nobody/nothing"}).status_code == 422


def test_the_real_client_factory_makes_one_client_per_model_only_when_asked_and_needs_a_key():
    keyless = Settings().client_factory()  # pyright: ignore[reportCallIssue]
    with pytest.raises(RuntimeError, match="OPENROUTER_API_KEY"):
        keyless(SONNET)

    factory = Settings(openrouter_api_key="sk-test").client_factory()  # pyright: ignore[reportCallIssue]
    sonnet = factory(SONNET)
    assert isinstance(sonnet, OpenRouterClient) and sonnet.model == SONNET
    assert factory(SONNET) is sonnet and factory(GLM) is not sonnet
