"""OpenRouterClient against a stubbed HTTP stream (httpx.MockTransport): parsing, usage/cost,
and mid-stream failures."""

import json

import httpx
import pytest

from flood_agent.llm import LLMError, OpenRouterClient, TextChunk, ToolCallChunk, UsageChunk


def sse(*objs, done=True):
    lines = [f"data: {o if isinstance(o, str) else json.dumps(o)}\n\n" for o in objs]
    if done:
        lines.append("data: [DONE]\n\n")
    return "".join(lines)


def client_for(body: str, status=200, seen=None) -> OpenRouterClient:
    def handler(request: httpx.Request):
        if seen is not None:
            seen.append(json.loads(request.content))
        return httpx.Response(status, text=body, headers={"content-type": "text/event-stream"})

    return OpenRouterClient("k", "m/x", transport=httpx.MockTransport(handler))


async def chunks(client):
    return [c async for c in client.stream_chat([{"role": "user", "content": "hi"}], [])]


def delta(**d):
    return {"choices": [{"delta": d}]}


def finish(reason="stop"):
    return {"choices": [{"delta": {}, "finish_reason": reason}]}


async def test_text_tool_calls_and_usage_are_parsed():
    body = sse(
        delta(content="Hel"), delta(content="lo"),
        delta(tool_calls=[{"index": 0, "id": "c1", "function": {"name": "run_query", "arguments": '{"sql"'}}]),
        delta(tool_calls=[{"index": 0, "function": {"arguments": ': "x"}'}}]),
        finish("tool_calls"),
        {"choices": [], "usage": {"prompt_tokens": 12, "completion_tokens": 3, "cost": 0.0042}},
    )
    seen = []
    out = await chunks(client_for(body, seen=seen))
    assert out == [TextChunk("Hel"), TextChunk("lo"),
                   ToolCallChunk("c1", "run_query", '{"sql": "x"}'), UsageChunk(12, 3, 0.0042)]
    assert seen[0]["usage"] == {"include": True} and seen[0]["stream"] is True


async def test_missing_cost_is_none():
    body = sse(delta(content="a"), finish(), {"choices": [], "usage": {"prompt_tokens": 1, "completion_tokens": 2}})
    assert (await chunks(client_for(body)))[-1] == UsageChunk(1, 2, None)


async def test_cached_prompt_tokens_are_read_from_the_usage_details():
    usage = {"prompt_tokens": 100, "completion_tokens": 2, "prompt_tokens_details": {"cached_tokens": 80}}
    body = sse(delta(content="a"), finish(), {"choices": [], "usage": usage})
    assert (await chunks(client_for(body)))[-1] == UsageChunk(100, 2, None, cached_tokens=80)


async def test_usage_without_cached_tokens_counts_zero():
    body = sse(delta(content="a"), finish(), {"choices": [], "usage": {"prompt_tokens": 1, "completion_tokens": 2}})
    assert (await chunks(client_for(body)))[-1].cached_tokens == 0
    usage = {"prompt_tokens": 1, "completion_tokens": 2, "prompt_tokens_details": None}
    body = sse(delta(content="a"), finish(), {"choices": [], "usage": usage})
    assert (await chunks(client_for(body)))[-1].cached_tokens == 0


async def test_mid_stream_error_chunk_raises():
    body = sse(delta(content="par"), {"error": {"code": 502, "message": "upstream exploded"}}, done=False)
    with pytest.raises(LLMError, match="upstream exploded"):
        await chunks(client_for(body))


async def test_finish_reason_error_raises():
    with pytest.raises(LLMError):
        await chunks(client_for(sse(delta(content="a"), finish("error"))))


async def test_stream_truncated_before_completion_raises():
    with pytest.raises(LLMError, match="before it was complete"):
        await chunks(client_for(sse(delta(content="partial"), done=False)))


async def test_finish_reason_without_done_is_complete():
    assert await chunks(client_for(sse(delta(content="ok"), finish(), done=False))) == [TextChunk("ok")]


async def test_incomplete_tool_call_arguments_raise():
    body = sse(delta(tool_calls=[{"index": 0, "id": "c1", "function": {"name": "run_query", "arguments": '{"sql": "SEL'}}]),
               finish("length"))
    with pytest.raises(LLMError, match="tool call"):
        await chunks(client_for(body))


async def test_malformed_chunk_raises():
    with pytest.raises(LLMError):
        await chunks(client_for(sse("{not json", done=False)))


# --- prompt caching: extra body fields chosen by model-ID prefix ---

OK = sse({"choices": [{"delta": {"content": "hi"}, "finish_reason": "stop"}]})


def test_claude_models_need_a_top_level_ephemeral_cache_control():
    from flood_agent.llm import caching_fields

    assert caching_fields("anthropic/claude-sonnet-5.5") == {"cache_control": {"type": "ephemeral"}}


def test_models_that_cache_automatically_need_no_extra_fields():
    from flood_agent.llm import caching_fields

    assert caching_fields("deepseek/deepseek-v4") == {}


async def request_body(model, reasoning=None):
    seen: list = []

    def handler(request: httpx.Request):
        seen.append(json.loads(request.content))
        return httpx.Response(200, text=OK, headers={"content-type": "text/event-stream"})

    c = OpenRouterClient("k", model, transport=httpx.MockTransport(handler))
    [x async for x in c.stream_chat([{"role": "user", "content": "hi"}], [], reasoning=reasoning)]
    return seen[0]


async def test_a_claude_request_body_carries_cache_control_and_keeps_reasoning():
    body = await request_body("anthropic/claude-sonnet-5.5", {"effort": "low"})

    assert body["cache_control"] == {"type": "ephemeral"}
    assert body["reasoning"] == {"effort": "low"}


@pytest.mark.parametrize("model", ["deepseek/deepseek-v4", "z-ai/glm-5.3", "qwen/qwen3-max"])
async def test_other_models_send_no_cache_control(model):
    assert "cache_control" not in await request_body(model)
