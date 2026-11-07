"""Choosing an Effort level: validated against the model's levels, and sent to the provider as a
`reasoning` setting only when it is not the model's default (checked at the request the provider receives)."""

import json

import httpx
import pytest
from conftest import ScriptedLLM

from flood_agent.agent import Conversation
from flood_agent.llm import OpenRouterClient

SONNET, HAIKU, GLM = "anthropic/claude-sonnet-5.5", "anthropic/claude-haiku-5.5", "z-ai/glm-5.3"  # defaults: high, medium, none


def post_chat(client, **data):
    return client.post("/chat", data={"message": "hello", "max_rows": "1000", **data})


def provider_bodies(build_agent):
    """An Agent whose real OpenRouterClient talks to a mock transport; returns (agent, requests seen)."""
    seen: list[dict] = []

    def handler(request: httpx.Request):
        seen.append(json.loads(request.content))
        body = 'data: {"choices": [{"delta": {"content": "hi"}, "finish_reason": "stop"}]}\n\ndata: [DONE]\n\n'
        return httpx.Response(200, text=body, headers={"content-type": "text/event-stream"})

    agent = build_agent(ScriptedLLM())
    agent.client_for = lambda model: OpenRouterClient("k", model, transport=httpx.MockTransport(handler))
    return agent, seen


async def turn(agent, model, effort):
    return [e async for e in agent.run_turn(Conversation(), "q", 1000, model, effort)]


@pytest.mark.parametrize("model, effort", [(SONNET, "low"), (SONNET, "max"), (HAIKU, "none")])
async def test_a_non_default_effort_reaches_the_provider_as_a_reasoning_setting(build_agent, model, effort):
    agent, seen = provider_bodies(build_agent)
    async with agent:
        await turn(agent, model, effort)

    assert seen and all(body["reasoning"] == {"effort": effort} for body in seen)


@pytest.mark.parametrize("model, effort", [(SONNET, "high"), (HAIKU, "medium"), (GLM, None), (SONNET, None)])
async def test_the_default_effort_sends_no_reasoning_setting(build_agent, model, effort):
    agent, seen = provider_bodies(build_agent)
    async with agent:
        await turn(agent, model, effort)

    assert seen and all("reasoning" not in body for body in seen)


def test_an_effort_the_model_does_not_offer_is_rejected_before_any_turn_starts(build_agent, web_client):
    llm = ScriptedLLM()
    agent = build_agent(llm)
    client = web_client(agent)

    for model, effort in [(SONNET, "none"), (SONNET, "bogus"), (GLM, "low")]:
        response = post_chat(client, model=model, effort=effort)
        assert response.status_code == 422 and "does not offer reasoning effort" in response.json()["detail"]

    assert llm.calls == [] and agent.store.list_conversations() == []


def test_a_supported_or_empty_effort_is_accepted(build_agent, web_client):
    client = web_client(build_agent(ScriptedLLM()))

    assert post_chat(client, model=SONNET, effort="max").status_code == 200
    assert post_chat(client, model=GLM, effort="").status_code == 200  # GLM's control is disabled: it sends none
