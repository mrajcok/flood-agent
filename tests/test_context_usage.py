"""Context Usage: how much of the model's window the Conversation fills, shown under the prompt box."""

import re

import pytest
from conftest import ScriptedLLM, tool_call

from flood_agent.context_usage import context_usage, format_tokens
from flood_agent.llm import TextChunk
from flood_agent.trace import context_tokens


@pytest.mark.parametrize(
    "n, text",
    [(0, "0"), (999, "999"), (1_000, "1k"), (187_400, "187k"), (999_999, "1M"), (1_000_000, "1M"), (1_050_000, "1.05M")],
)
def test_format_tokens(n, text):
    assert format_tokens(n) == text


@pytest.mark.parametrize(
    "tokens, level", [(0, ""), (399_999, ""), (400_000, "amber"), (699_999, "amber"), (700_000, "red"), (1_200_000, "red")]
)
def test_levels_turn_amber_at_40_percent_and_red_at_70(tokens, level):
    assert context_usage(tokens, 1_000_000)["level"] == level


def test_usage_text_and_bar_width():
    usage = context_usage(187_000, 1_000_000)
    assert usage["text"] == "187k of 1M" and usage["percent"] == 18.7
    assert context_usage(2_000_000, 1_000_000)["percent"] == 100  # the bar never overflows


def test_no_window_means_no_context_is_used():
    assert context_usage(5000, 0) == {"text": "no context used", "percent": 0, "level": "", "window": 0}


def test_no_calls_yet_is_zero():
    assert context_usage(None, 1_000_000)["text"] == "0 of 1M"


def test_context_tokens_is_the_latest_llm_steps_prompt_tokens():
    trace = {"entries": [
        {"kind": "llm_step", "usage": {"prompt_tokens": 100}},
        {"kind": "tool_call"},
        {"kind": "llm_step", "usage": {"prompt_tokens": 250}},
        {"kind": "flag"},
    ]}
    assert context_tokens(trace) == 250
    assert context_tokens({"entries": [{"kind": "llm_step", "usage": None}]}) is None
    assert context_tokens({"entries": []}) is None


def ask(client, message, conversation_id=""):
    posted = client.post("/chat", data={"message": message, "conversation_id": conversation_id, "max_rows": "1000"})
    cid = re.search(r'name="conversation_id" value="([0-9a-f]+)"', posted.text).group(1)
    stream_url = posted.text.split('sse-connect="')[1].split('"')[0]
    return cid, client.get(stream_url).text


def meter(html):
    return re.search(r'<span id="context-meter"[^>]*>(.*?)</span>', html, re.S)


def test_a_new_page_shows_zero_used(build_agent, web_client):
    page = web_client(build_agent(ScriptedLLM())).get("/").text
    assert "0 of 1.05M" in meter(page).group(1)


def test_a_reopened_conversation_shows_its_latest_calls_prompt_tokens(build_agent, web_client):
    client = web_client(build_agent(ScriptedLLM([tool_call("run_query", sql="SELECT 1 AS x", title="t")], [TextChunk("ok")])))
    cid, _ = ask(client, "hello")
    assert "100 of 1.05M" in meter(client.get(f"/c/{cid}").text).group(1)  # ScriptedLLM reports 100 prompt tokens


def test_the_stream_refreshes_the_meter_in_place_when_the_turn_ends(build_agent, web_client):
    client = web_client(build_agent(ScriptedLLM([TextChunk("hi")])))
    _, body = ask(client, "hello")
    assert 'id="context-meter"' in body and 'hx-swap-oob="true"' in body and "100 of 1.05M" in body


def test_fake_mode_has_no_context_window(build_agent, web_client):
    page = web_client(build_agent(ScriptedLLM()), fake_mode=True).get("/").text
    assert "no context used" in meter(page).group(1)
