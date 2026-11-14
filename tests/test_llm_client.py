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


async def test_cache_write_tokens_are_read_from_the_usage_details():
    details = {"cached_tokens": 10, "cache_write_tokens": 70}
    usage = {"prompt_tokens": 100, "completion_tokens": 2, "prompt_tokens_details": details}
    body = sse(delta(content="a"), finish(), {"choices": [], "usage": usage})
    last = (await chunks(client_for(body)))[-1]
    assert last.cache_write_tokens == 70 and last.cached_tokens == 10


async def test_usage_without_cache_write_tokens_counts_zero():
    body = sse(delta(content="a"), finish(), {"choices": [], "usage": {"prompt_tokens": 1, "completion_tokens": 2}})
    assert (await chunks(client_for(body)))[-1].cache_write_tokens == 0
    details = {"cached_tokens": 5, "cache_write_tokens": None}
    usage = {"prompt_tokens": 1, "completion_tokens": 2, "prompt_tokens_details": details}
    body = sse(delta(content="a"), finish(), {"choices": [], "usage": usage})
    assert (await chunks(client_for(body)))[-1].cache_write_tokens == 0


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


def test_claude_models_need_a_top_level_ephemeral_cache_control_and_prefer_anthropics_own_endpoint():
    from flood_agent.llm import caching_fields

    # Google Vertex cannot read a cache entry written seconds earlier, which is every step of a tool-call
    # turn; Anthropic's endpoint can. A preference, not a pin: with fallbacks on, an account that does not
    # allow Anthropic still gets an answer (uncached) from another provider.
    assert caching_fields("anthropic/claude-sonnet-5.5") == {
        "cache_control": {"type": "ephemeral"},
        "provider": {"order": ["anthropic"]},
    }


@pytest.mark.parametrize("model", ["openai/gpt-5.6-terra", "deepseek/deepseek-v4", "z-ai/glm-5.3", "qwen/qwen3.8-27b"])
def test_only_claude_models_state_a_provider_preference(model):
    from flood_agent.llm import caching_fields

    assert "provider" not in caching_fields(model, session_id="c7")


async def test_a_claude_request_body_prefers_anthropic_without_forbidding_other_providers():
    body = await request_body("anthropic/claude-sonnet-5.5")

    assert body["provider"] == {"order": ["anthropic"]}
    assert "allow_fallbacks" not in body["provider"]  # OpenRouter's default is to fall back


def test_models_that_cache_automatically_need_no_extra_fields():
    from flood_agent.llm import caching_fields

    assert caching_fields("deepseek/deepseek-v4") == {}


@pytest.mark.parametrize("model", ["z-ai/glm-5.3", "qwen/qwen3.8-27b"])
def test_glm_and_qwen_need_no_cache_control(model):
    from flood_agent.llm import caching_fields

    assert "cache_control" not in caching_fields(model, session_id="c7")


SESSION_PREFIXES = ["anthropic/claude-sonnet-5.5", "openai/gpt-5.6-terra", "qwen/qwen3.8-27b",
                    "deepseek/deepseek-v4", "z-ai/glm-5.3"]


@pytest.mark.parametrize("model", SESSION_PREFIXES)
def test_models_that_benefit_get_the_exchange_id_as_session_id(model):
    from flood_agent.llm import caching_fields

    assert caching_fields(model, session_id="c7")["session_id"] == "c7"


def test_no_session_id_is_sent_without_an_exchange_id():
    from flood_agent.llm import caching_fields

    assert "session_id" not in caching_fields("z-ai/glm-5.3")


def test_unlisted_models_never_get_a_session_id():
    from flood_agent.llm import caching_fields

    assert caching_fields("meta-llama/llama-4", session_id="c7") == {}


@pytest.mark.parametrize("model", SESSION_PREFIXES)
async def test_session_id_is_stable_within_an_exchange_and_differs_between(model):
    a1 = await request_body(model, session_id="c1")
    a2 = await request_body(model, session_id="c1")
    b = await request_body(model, session_id="c2")

    assert a1["session_id"] == a2["session_id"] == "c1"
    assert b["session_id"] == "c2"


async def test_an_unlisted_models_request_body_is_unchanged():
    assert "session_id" not in await request_body("meta-llama/llama-4", session_id="c1")


async def request_body(model, reasoning=None, **kw):
    seen: list = []

    def handler(request: httpx.Request):
        seen.append(json.loads(request.content))
        return httpx.Response(200, text=OK, headers={"content-type": "text/event-stream"})

    c = OpenRouterClient("k", model, transport=httpx.MockTransport(handler))
    [x async for x in c.stream_chat([{"role": "user", "content": "hi"}], [], reasoning=reasoning, **kw)]
    return seen[0]


async def test_a_claude_request_body_carries_cache_control_and_keeps_reasoning():
    body = await request_body("anthropic/claude-sonnet-5.5", {"effort": "low"})

    assert body["cache_control"] == {"type": "ephemeral"}
    assert body["reasoning"] == {"effort": "low"}


@pytest.mark.parametrize("model", ["deepseek/deepseek-v4", "z-ai/glm-5.3", "qwen/qwen3-max"])
async def test_other_models_send_no_cache_control(model):
    assert "cache_control" not in await request_body(model)


async def test_a_glm_request_body_carries_the_session_id():
    body = await request_body("z-ai/glm-5.3", session_id="c7")

    assert body["session_id"] == "c7"

