"""Fake Mode: the scripted LLM stand-in, its Canned Prompts, and the page that lists them."""

import json
import re
import sqlite3

import pytest
from conftest import all_telemetry_values, collect, text_of

from flood_defender.__main__ import build_app, picker_entries
from flood_defender.agent import Conversation, DisplayEvent, TraceReady, TurnError
from flood_defender.config import Settings
from flood_defender.fake_llm import CannedPrompt, FakeClient, Step, load_canned_prompts, parent_of
from flood_defender.llm import TextChunk

CANNED = load_canned_prompts()
PICKER = picker_entries(CANNED)


def fake_client(canned=CANNED) -> FakeClient:
    return FakeClient(canned, chunk_delay_s=0)


class Recorder:
    """Wraps a client and keeps every message list it receives (for the leak check)."""

    def __init__(self, inner):
        self.inner, self.calls = inner, []

    def stream_chat(self, messages, tools):
        self.calls.append(json.dumps(messages))
        return self.inner.stream_chat(messages, tools)


@pytest.mark.parametrize("canned", CANNED, ids=lambda c: c.id)
async def test_every_canned_prompt_produces_its_declared_outcome(canned, make_agent, telemetry_db):
    llm = Recorder(fake_client())
    agent = await make_agent(llm, model="fake", max_result_rows=20_000)  # above the chart cap, yet small enough to trip fast
    conversation = Conversation()
    parent = parent_of(canned, CANNED)
    if parent:
        await collect(agent, parent.prompt, conversation)

    events = await collect(agent, canned.prompt, conversation)

    assert [e.kind for e in events if isinstance(e, DisplayEvent)] == list(canned.expect.displays)
    trace = next(e.trace for e in events if isinstance(e, TraceReady))
    assert trace["model"] == "fake"
    totals = trace["totals"]
    assert (totals["prompt_tokens"], totals["completion_tokens"]) == (0, 0)
    # an errored stream reports no usage, so its Trace honestly shows cost n/a
    assert totals["cost"] == (None if canned.expect.turn_error else 0)
    failed = [
        e for e in trace["entries"] if e["kind"] == "tool_call" and not json.loads(e["query_metadata"]).get("success")
    ]
    assert len(failed) == canned.expect.failed_tool_calls
    text = text_of(events)
    assert canned.expect.notice in text
    assert any(isinstance(e, TurnError) for e in events) == canned.expect.turn_error
    values = {v for v in all_telemetry_values(telemetry_db) if len(v) >= 4}
    sent = [re.sub(r"rt_\w+", "", call) for call in llm.calls]  # Result Table names are random, not telemetry
    assert not any(v in call for call in sent for v in values)


async def test_an_unmatched_message_gets_a_reply_naming_fake_mode_and_listing_prompts(make_agent):
    agent = await make_agent(fake_client(), model="fake")

    events = await collect(agent, "what is the weather?")

    text = text_of(events)
    assert "Fake Mode" in text
    assert all(c.prompt in text for c in CANNED)
    assert not [e for e in events if isinstance(e, DisplayEvent)]


async def test_a_failed_query_without_telemetry_data_says_so_instead_of_asking_for_an_earlier_prompt(
    make_agent, telemetry_db
):
    with sqlite3.connect(telemetry_db) as con:  # this test's own copy: as if the data was never generated
        con.execute("DROP TABLE flows")
    agent = await make_agent(fake_client(), model="fake")
    flow_count = next(c for c in CANNED if c.id == "flow-count")

    events = await collect(agent, flow_count.prompt)

    text = text_of(events)
    assert "run an earlier prompt" not in text
    assert "query failed" in text
    assert "flood_defender.generator" in text
    assert not [e for e in events if isinstance(e, DisplayEvent)]


def test_fake_mode_page_shows_badge_and_clickable_canned_prompts(build_agent, web_client):
    client = web_client(build_agent(fake_client(), model="fake"), picker=PICKER, fake_mode=True)

    html = client.get("/").text

    assert "Fake Mode — no LLM" in html
    assert CANNED[0].prompt in html
    created = client.post("/chat", data={"message": CANNED[0].prompt}).text
    cid = created.split('name="conversation_id" value="')[1].split('"')[0]
    reopened = client.get(f"/c/{cid}").text
    assert "Fake Mode — no LLM" in reopened and CANNED[0].prompt in reopened


def test_real_mode_page_has_neither_badge_nor_list(build_agent, web_client):
    html = web_client(build_agent(fake_client())).get("/").text

    assert "Fake Mode" not in html
    assert CANNED[0].prompt not in html


def test_fake_backend_builds_without_an_api_key(tmp_path, telemetry_db, semantic_layer_path):
    settings = Settings(  # pyright: ignore[reportCallIssue]
        llm_backend="fake",
        telemetry_db=telemetry_db,
        results_db=tmp_path / "results.db",
        app_db=tmp_path / "app.db",
        semantic_layer=semantic_layer_path,
    )

    assert build_app(settings) is not None


def test_openrouter_backend_still_requires_an_api_key():
    with pytest.raises(RuntimeError, match="OPENROUTER_API_KEY"):
        build_app(Settings(llm_backend="openrouter"))  # pyright: ignore[reportCallIssue]


async def test_a_follow_up_picked_first_replies_to_run_its_parent_and_makes_no_tool_call(make_agent):
    follow_up = next(c for c in CANNED if c.after)
    parent = parent_of(follow_up, CANNED)
    assert parent
    agent = await make_agent(fake_client(), model="fake")

    events = await collect(agent, follow_up.prompt)

    assert f"Fake Mode: run *{parent.prompt}* first in this Conversation" in text_of(events)
    assert not [e for e in events if isinstance(e, DisplayEvent)]


async def test_a_follow_up_after_an_unrelated_prompt_replies_parent_first_and_makes_no_tool_call(make_agent):
    follow_up = next(c for c in CANNED if c.after)
    parent = parent_of(follow_up, CANNED)
    unrelated = next(c for c in CANNED if c.id == "flow-count")
    assert parent and parent is not unrelated
    agent = await make_agent(fake_client(), model="fake")
    conversation = Conversation()
    await collect(agent, unrelated.prompt, conversation)

    events = await collect(agent, follow_up.prompt, conversation)

    assert f"Fake Mode: run *{parent.prompt}* first in this Conversation" in text_of(events)
    assert not [e for e in events if isinstance(e, DisplayEvent)]
    trace = next(e.trace for e in events if isinstance(e, TraceReady))
    assert not [e for e in trace["entries"] if e["kind"] == "tool_call"]


async def test_malformed_tool_result_json_is_not_reported_as_run_parent_first():
    follow_up = next(c for c in CANNED if c.after)
    parent = parent_of(follow_up, CANNED)
    assert parent
    messages = [
        {"role": "user", "content": parent.prompt},
        {"role": "assistant", "content": ""},
        {"role": "tool", "content": "not json"},
        {"role": "user", "content": follow_up.prompt},
    ]

    with pytest.raises(ValueError):
        [c async for c in fake_client().stream_chat(messages, [])]


async def test_a_script_that_runs_out_of_steps_ends_the_turn_with_a_short_reply():
    ad_hoc = CannedPrompt(id="x", prompt="ad hoc", steps=(Step(text="only step"),))
    messages = [{"role": "user", "content": "ad hoc"}, {"role": "assistant", "content": "only step"}]

    chunks = [c async for c in fake_client([ad_hoc]).stream_chat(messages, [])]

    assert "".join(c.text for c in chunks if isinstance(c, TextChunk)) == "Fake Mode: script ended."


def test_an_unknown_key_in_a_canned_prompt_is_a_load_error(tmp_path):
    path = tmp_path / "canned.yaml"
    path.write_text("- {id: a, prompt: A, steps: [{txt: oops}]}\n")

    with pytest.raises(TypeError, match="txt"):
        load_canned_prompts(path)


def test_a_dangling_after_is_a_load_error(tmp_path):
    path = tmp_path / "canned.yaml"
    path.write_text("- {id: a, prompt: A, steps: [], after: missing}\n")

    with pytest.raises(ValueError, match="missing"):
        load_canned_prompts(path)


def test_picker_indents_follow_ups_under_their_parent(build_agent, web_client):
    follow_up = next(c for c in CANNED if c.after)
    parent = parent_of(follow_up, CANNED)
    assert parent
    client = web_client(build_agent(fake_client(), model="fake"), picker=PICKER, fake_mode=True)

    html = client.get("/").text

    assert html.count('class="follow-up"') == sum(1 for c in CANNED if c.after)
    parent_at = html.index(parent.prompt)
    assert parent_at < html.index('class="follow-up"')


def test_picker_marks_prompts_that_fail_on_purpose(build_agent, web_client):
    html = web_client(build_agent(fake_client(), model="fake"), picker=PICKER, fake_mode=True).get("/").text

    marked = [c for c in CANNED if c.fails_on_purpose]
    assert marked and len(marked) < len(CANNED)
    assert html.count("fails on purpose") == len(marked)

    def row(c):
        return html.split(c.prompt, 1)[1].split("</li>", 1)[0]

    assert all("fails on purpose" in row(c) for c in marked)
    assert not any("fails on purpose" in row(c) for c in CANNED if not c.fails_on_purpose)
