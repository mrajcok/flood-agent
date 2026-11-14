"""Choosing an Effort level: validated against the model's levels, and sent to the provider as a
`reasoning` setting only when it is not the model's default (checked at the request the provider receives)."""

import json

import httpx
import pytest
from conftest import ScriptedLLM, collect, post_run

from flood_agent.llm import OpenRouterClient

SONNET, HAIKU, GLM = "anthropic/claude-sonnet-5.5", "anthropic/claude-haiku-5.5", "z-ai/glm-5.3"  # defaults: high, medium, maxne


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


async def turn(agent, model, effort, notebook=None):
    return await collect(agent, "q", notebook, 1000, model, effort)


@pytest.mark.parametrize("model, effort", [(SONNET, "low"), (SONNET, "max"), (HAIKU, "none")])
async def test_a_non_default_effort_reaches_the_provider_as_a_reasoning_setting(build_agent, model, effort):
    agent, seen = provider_bodies(build_agent)
    async with agent:
        await turn(agent, model, effort)

    assert seen and all(body["reasoning"] == {"effort": effort} for body in seen)


@pytest.mark.parametrize("model, effort", [(SONNET, "high"), (HAIKU, "medium"), (GLM, "max"), (GLM, None), (SONNET, None)])
async def test_the_default_effort_sends_no_reasoning_setting(build_agent, model, effort):
    agent, seen = provider_bodies(build_agent)
    async with agent:
        await turn(agent, model, effort)

    assert seen and all("reasoning" not in body for body in seen)


def test_an_effort_the_model_does_not_offer_is_rejected_before_any_turn_starts(build_agent, web_client):
    llm = ScriptedLLM()
    agent = build_agent(llm)
    client = web_client(agent)

    for model, effort in [(SONNET, "none"), (SONNET, "bogus"), (GLM, "none")]:
        response = post_run(client, model=model, effort=effort)
        assert response.status_code == 422 and "does not offer reasoning effort" in response.json()["detail"]

    assert llm.calls == [] and all(
        c["run"] is None for n in agent.notebooks.list_notebooks() for c in agent.notebooks.cells(n["id"]))


def test_a_supported_or_empty_effort_is_accepted(build_agent, web_client):
    client = web_client(build_agent(ScriptedLLM()))

    assert post_run(client, model=SONNET, effort="max").status_code == 200
    assert post_run(client, model=GLM, effort="").status_code == 200  # empty means the model's default


SESSION_MODELS = [SONNET, "openai/gpt-5.6-terra", "qwen/qwen3.8-27b", "deepseek/deepseek-v4", GLM]


@pytest.mark.parametrize("model", SESSION_MODELS)
async def test_requests_share_a_session_id_within_a_notebook_and_differ_between_notebooks(build_agent, model):
    agent, seen = provider_bodies(build_agent)
    async with agent:
        first, second = agent.create_notebook(), agent.create_notebook()
        for notebook in (first, second, first):
            await turn(agent, model, None, notebook)

    ids = [body["session_id"] for body in seen]
    assert len(ids) == 3 and ids[0] == ids[2] and ids[0] != ids[1]


async def test_an_unlisted_model_gets_no_session_id(build_agent):
    agent, seen = provider_bodies(build_agent)
    async with agent:
        await turn(agent, "meta-llama/llama-4", None)

    assert seen and all("session_id" not in body for body in seen)
