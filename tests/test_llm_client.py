"""OpenRouterClient against a stubbed HTTP stream (httpx.MockTransport): parsing, usage/cost,
and mid-stream failures."""

import json

import httpx
import pytest

from flood_defender.llm import LLMError, OpenRouterClient, TextChunk, ToolCallChunk, UsageChunk


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
