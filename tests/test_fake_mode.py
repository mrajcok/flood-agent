"""Fake Mode: the scripted LLM stand-in, its Canned Prompts, and the page that lists them."""

import json

import pytest
from conftest import all_telemetry_values, collect

from flood_defender.__main__ import build_app
from flood_defender.agent import Conversation, DisplayEvent, TextDelta, TraceReady, TurnError
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
    agent = await make_agent(llm, model="fake", max_result_rows=20_000)  # above the chart cap, yet small enough to trip fast
    conversation = Conversation()
    parent = next((c for c in CANNED if c.id == canned.after), None)
    if parent:
        await collect(agent, parent.prompt, conversation)

    events = await collect(agent, canned.prompt, conversation)

    assert [e.kind for e in events if isinstance(e, DisplayEvent)] == canned.expect["displays"]
    trace = next(e.trace for e in events if isinstance(e, TraceReady))
    assert trace["model"] == "fake"
    totals = trace["totals"]
    assert (totals["prompt_tokens"], totals["completion_tokens"], totals["cost"] or 0) == (0, 0, 0)
    failed = [
        e for e in trace["entries"] if e["kind"] == "tool_call" and not json.loads(e["query_metadata"]).get("success")
    ]
    assert len(failed) == canned.expect.get("failed_tool_calls", 0)
    text = "".join(e.text for e in events if isinstance(e, TextDelta))
    assert canned.expect.get("notice", "") in text
    assert any(isinstance(e, TurnError) for e in events) == canned.expect.get("turn_error", False)
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


async def test_a_follow_up_picked_first_replies_to_run_its_parent_and_makes_no_tool_call(make_agent):
    follow_up = next(c for c in CANNED if c.after)
    parent = next(c for c in CANNED if c.id == follow_up.after)
    agent = await make_agent(FakeClient(CANNED), model="fake")

    events = await collect(agent, follow_up.prompt)

    text = "".join(e.text for e in events if isinstance(e, TextDelta))
    assert f"Fake mode: run {parent.prompt} first in this Conversation" in text
    assert not [e for e in events if isinstance(e, DisplayEvent)]


def test_a_dangling_after_is_a_load_error(tmp_path):
    path = tmp_path / "canned.yaml"
    path.write_text("- {id: a, prompt: A, steps: [], after: missing}\n")

    with pytest.raises(ValueError, match="missing"):
        load_canned_prompts(path)


def test_picker_indents_follow_ups_under_their_parent(build_agent, web_client):
    follow_up = next(c for c in CANNED if c.after)
    client = web_client(build_agent(FakeClient(CANNED), model="fake"), canned_prompts=CANNED)

    html = client.get("/").text

    assert html.count('class="follow-up"') == sum(1 for c in CANNED if c.after)
    parent_at = html.index(next(c for c in CANNED if c.id == follow_up.after).prompt)
    assert parent_at < html.index('class="follow-up"')


def test_picker_marks_prompts_that_fail_on_purpose(build_agent, web_client):
    html = web_client(build_agent(FakeClient(CANNED), model="fake"), canned_prompts=CANNED).get("/").text

    marked = [c for c in CANNED if c.fails_on_purpose]
    assert marked and len(marked) < len(CANNED)
    assert html.count("fails on purpose") == len(marked)

    def row(c):
        return html.split(c.prompt, 1)[1].split("</li>", 1)[0]

    assert all("fails on purpose" in row(c) for c in marked)
    assert not any("fails on purpose" in row(c) for c in CANNED if not c.fails_on_purpose)
