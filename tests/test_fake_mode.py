"""Fake Mode: the scripted LLM stand-in, its Canned Prompts, and the page that lists them."""

import json

import pytest
from conftest import all_telemetry_values, collect

from flood_defender.__main__ import build_app
from flood_defender.agent import Conversation, DisplayEvent, TextDelta, TraceReady
from flood_defender.config import Settings
from flood_defender.fake_llm import FakeClient, load_canned_prompts

CANNED = load_canned_prompts()


class Recorder:
    """Wraps a client and keeps every message list it receives (for the leak check)."""

    def __init__(self, inner):
        self.inner, self.calls = inner, []

    def stream_chat(self, messages, tools):
        self.calls.append(json.dumps(messages))
        return self.inner.stream_chat(messages, tools)


@pytest.mark.parametrize("canned", CANNED, ids=lambda c: c.id)
async def test_every_canned_prompt_produces_its_declared_outcome(canned, make_agent, telemetry_db):
    llm = Recorder(FakeClient(CANNED))
    agent = await make_agent(llm, model="fake")
    conversation = Conversation()
    parent = next((c for c in CANNED if c.id == canned.after), None)
    if parent:
        await collect(agent, parent.prompt, conversation)

    events = await collect(agent, canned.prompt, conversation)

    assert [e.kind for e in events if isinstance(e, DisplayEvent)] == canned.expect["displays"]
    trace = next(e.trace for e in events if isinstance(e, TraceReady))
    assert trace["model"] == "fake"
    totals = trace["totals"]
    assert (totals["prompt_tokens"], totals["completion_tokens"], totals["cost"]) == (0, 0, 0)
    values = {v for v in all_telemetry_values(telemetry_db) if len(v) >= 4}
    assert not any(v in call for call in llm.calls for v in values)


async def test_an_unmatched_message_gets_a_reply_naming_fake_mode_and_listing_prompts(make_agent):
    agent = await make_agent(FakeClient(CANNED), model="fake")

    events = await collect(agent, "what is the weather?")

    text = "".join(e.text for e in events if isinstance(e, TextDelta))
    assert "Fake Mode" in text
    assert all(c.prompt in text for c in CANNED)
    assert not [e for e in events if isinstance(e, DisplayEvent)]


def test_fake_mode_page_shows_badge_and_clickable_canned_prompts(build_agent, web_client):
    client = web_client(build_agent(FakeClient(CANNED), model="fake"), canned_prompts=CANNED)

    html = client.get("/").text

    assert "Fake mode — no LLM" in html
    assert CANNED[0].prompt in html
    created = client.post("/chat", data={"message": CANNED[0].prompt}).text
    cid = created.split('name="conversation_id" value="')[1].split('"')[0]
    reopened = client.get(f"/c/{cid}").text
    assert "Fake mode — no LLM" in reopened and CANNED[0].prompt in reopened


def test_real_mode_page_has_neither_badge_nor_list(build_agent, web_client):
    html = web_client(build_agent(FakeClient(CANNED))).get("/").text

    assert "Fake mode" not in html
    assert CANNED[0].prompt not in html


def test_fake_backend_builds_without_an_api_key():
    assert build_app(Settings(llm_backend="fake")) is not None  # pyright: ignore[reportCallIssue]


def test_openrouter_backend_still_requires_an_api_key():
    with pytest.raises(RuntimeError, match="OPENROUTER_API_KEY"):
        build_app(Settings(llm_backend="openrouter"))  # pyright: ignore[reportCallIssue]
