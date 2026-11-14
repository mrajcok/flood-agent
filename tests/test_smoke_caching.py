"""The caching smoke check's verdict logic (the script itself calls the real API and is run by hand)."""

import json

import httpx
from pydantic import SecretStr

from flood_agent.config import Settings
from flood_agent.evalset.golden import Question
from flood_agent.evalset.smoke_caching import Step, judge, judge_across_cells, prefix_difference, run_notebook

CC = {"cache_control": {"type": "ephemeral"}}


def step(prompt=2000, cached=0, write=0, sent=CC, raw=None):
    return Step(prompt_tokens=prompt, cached_tokens=cached, cache_write_tokens=write, sent=sent, raw_usage=raw)


def test_a_write_then_a_read_is_working():
    verdict, why = judge([step(write=1900), step(cached=1900)], expects_cache_control=True)

    assert verdict == "WORKING" and "wrote 1900" in why


def test_a_read_on_step_two_is_working_even_if_writes_are_not_reported():
    assert judge([step(), step(cached=1800)], expects_cache_control=True)[0] == "WORKING"


def test_a_warm_cache_from_an_earlier_run_is_working_and_says_so():
    verdict, why = judge([step(cached=1800), step(cached=1900)], expects_cache_control=True)

    assert verdict == "WORKING" and "warm" in why


def test_no_reads_is_not_working():
    verdict, why = judge([step(), step(raw={"prompt_tokens_details": {"cached_tokens": 0}})], expects_cache_control=True)

    assert verdict == "NOT WORKING" and "not applied" in why


def test_usage_without_cache_fields_says_the_provider_reports_none():
    verdict, why = judge([step(), step(raw={"prompt_tokens": 2000})], expects_cache_control=True)

    assert verdict == "NOT WORKING" and "no prompt_tokens_details" in why


def test_a_claude_request_without_cache_control_is_not_working():
    verdict, why = judge([step(sent={}), step(sent={})], expects_cache_control=True)

    assert verdict == "NOT WORKING" and "cache_control" in why


def test_a_non_claude_model_needs_no_cache_control():
    assert judge([step(sent={}), step(sent={}, cached=1800)], expects_cache_control=False)[0] == "WORKING"


def test_a_single_step_turn_is_inconclusive():
    assert judge([step()], expects_cache_control=True)[0] == "INCONCLUSIVE"


def test_a_prompt_under_the_minimum_is_inconclusive():
    assert judge([step(prompt=300), step(prompt=300)], expects_cache_control=True)[0] == "INCONCLUSIVE"


def test_a_later_request_that_extends_the_earlier_prompt_shares_its_prefix():
    first = {"tools": [{"n": 1}], "messages": [{"role": "system", "content": "s"}, {"role": "user", "content": "q"}]}
    second = {**first, "messages": [*first["messages"], {"role": "assistant", "content": "a"}]}

    assert prefix_difference(first, second) is None


def test_a_changed_earlier_message_breaks_the_shared_prefix():
    first = {"tools": [], "messages": [{"role": "system", "content": "now 12:00"}, {"role": "user", "content": "q"}]}
    second = {"tools": [], "messages": [{"role": "system", "content": "now 12:01"}, {"role": "user", "content": "q"}]}

    assert "message 0" in (prefix_difference(first, second) or "")


def test_changed_tool_definitions_break_the_shared_prefix():
    assert prefix_difference({"tools": [1], "messages": []}, {"tools": [2], "messages": []}) == "the tool definitions differ"


def test_the_system_experiment_moves_cache_control_onto_the_system_block():
    from flood_agent.evalset.smoke_caching import apply_experiment

    body = {"cache_control": {"type": "ephemeral"}, "messages": [{"role": "system", "content": "s"}, {"role": "user", "content": "q"}]}
    out = apply_experiment(body, None, "system")

    assert "cache_control" not in out
    assert out["messages"][0]["content"] == [{"type": "text", "text": "s", "cache_control": {"type": "ephemeral"}}]
    assert body["cache_control"] and body["messages"][0]["content"] == "s"  # the input is not changed


def test_the_provider_experiment_pins_the_provider_and_the_default_changes_nothing():
    from flood_agent.evalset.smoke_caching import apply_experiment

    body = {"messages": []}

    assert apply_experiment(body, "Anthropic", "top")["provider"] == {"order": ["Anthropic"], "allow_fallbacks": False}
    assert apply_experiment(body, None, "top") == body


def test_tool_activity_names_the_tool_called_and_the_result_fed_back():
    from flood_agent.evalset.smoke_caching import tool_activity

    first = Step(body={"messages": [{"role": "user", "content": "q"}]})
    last = Step(body={"messages": [
        {"role": "user", "content": "q"},
        {"role": "assistant", "tool_calls": [{"function": {"name": "run_count_query"}}]},
        {"role": "tool", "content": "42"},
    ]})

    assert tool_activity([first, last]) == "assistant called run_count_query; tool result (2 chars)"
    assert "fewer than 2" in tool_activity([first])


# ---- the notebook harness, offline against a simulated provider ----------------------------------------


def _sse(*payloads) -> bytes:
    return b"".join(b"data: " + json.dumps(p).encode() + b"\n\n" for p in payloads) + b"data: [DONE]\n\n"


class SimulatedProvider(httpx.AsyncBaseTransport):
    """Stands in for OpenRouter: asks for a count the first time it is called with a question and answers once it
    has the result; reports a cache read for the longest run of leading messages an earlier request also sent
    (when the request carries cache_control), like a provider with a working prefix cache."""

    def __init__(self, cache_works: bool = True):
        self.seen: list[dict] = []
        self.cache_works = cache_works

    @staticmethod
    def tokens(messages) -> int:
        return len(json.dumps(messages)) // 4

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        messages = body["messages"]
        shared = 0
        for earlier in self.seen:
            n = 0
            while n < min(len(messages), len(earlier["messages"])) and messages[n] == earlier["messages"][n]:
                n += 1
            shared = max(shared, n)
        self.seen.append(body)
        cached = self.tokens(messages[:shared]) if self.cache_works and "cache_control" in body else 0
        usage = {"prompt_tokens": max(self.tokens(messages), 1), "completion_tokens": 5, "cost": 0.001,
                 "prompt_tokens_details": {"cached_tokens": cached, "cache_write_tokens": 0}}
        if messages[-1]["role"] == "tool":
            delta = {"content": "done"}
        else:
            call = {"index": 0, "id": f"c{len(self.seen)}", "function": {"name": "run_count_query", "arguments": json.dumps(
                {"sql": "SELECT COUNT(*) FROM flows", "title": "flows"})}}
            delta = {"tool_calls": [call]}
        return httpx.Response(200, content=_sse({"choices": [{"delta": delta, "finish_reason": "stop"}]}, {"usage": usage, "choices": []}))


def _settings(telemetry_db, semantic_layer_path, tmp_path):
    return Settings.from_defaults().model_copy(update={
        "openrouter_api_key": SecretStr("test-key"), "telemetry_db": telemetry_db, "semantic_layer": semantic_layer_path})


QUESTIONS = [
    Question(id="a", question="How many flows?", sql="SELECT 1"),
    Question(id="b", question="Which port is first above?", sql="SELECT 1",
             above=[{"kind": "sql", "sql": "SELECT COUNT(*) FROM flows"},  # a bare count makes no Result Table
                    {"kind": "markdown", "text": "Ports only.", "send_to_ai": True},
                    {"kind": "show", "command": "DESCRIBE TABLE", "table": "flows"}]),
]


async def test_the_second_ai_cell_continues_the_first_ones_prompt_and_reads_its_cache(telemetry_db, semantic_layer_path, tmp_path):
    cells, error, reply = await run_notebook("anthropic/claude-sonnet-5.5", QUESTIONS,
                                             _settings(telemetry_db, semantic_layer_path, tmp_path), tmp_path / "w",
                                             inner=SimulatedProvider())

    assert error is None and reply == "done" and [len(c) for c in cells] == [2, 2]
    first, second = cells
    assert first[0].sent["session_id"] == second[0].sent["session_id"]
    assert first[0].sent["session_id"].startswith("notebook-")
    assert prefix_difference(first[-1].body, second[0].body) is None
    texts = [m["content"] for m in second[0].body["messages"] if m["role"] == "user"]
    assert any(t.startswith("[SQL Cell, run by the user] SELECT COUNT") for t in texts) and "Ports only." in " ".join(texts)
    assert any(t.startswith("[SHOW Cell, run by the user] DESCRIBE TABLE flows") for t in texts)
    assert judge_across_cells(first, second)[0] == "WORKING"


async def test_a_provider_that_does_not_cache_fails_the_cross_cell_check(telemetry_db, semantic_layer_path, tmp_path):
    cells, _, _ = await run_notebook("anthropic/claude-sonnet-5.5", QUESTIONS,
                                     _settings(telemetry_db, semantic_layer_path, tmp_path), tmp_path / "w",
                                     inner=SimulatedProvider(cache_works=False))

    verdict, why = judge_across_cells(*cells)
    assert verdict == "NOT WORKING" and "no cached tokens" in why


def test_a_second_cell_whose_prompt_does_not_start_with_the_firsts_last_request_fails():
    last = Step(prompt_tokens=2000, sent={"session_id": "notebook-1"}, body={"tools": [], "messages": [{"role": "system", "content": "a"}]})
    first = Step(prompt_tokens=2000, cached_tokens=900, sent={"session_id": "notebook-1"}, body={"tools": [], "messages": [{"role": "system", "content": "b"}]})

    verdict, why = judge_across_cells([last], [first])
    assert verdict == "NOT WORKING" and "message 0" in why


def test_cells_with_different_session_ids_fail():
    body = {"tools": [], "messages": [{"role": "system", "content": "a"}]}
    last = Step(prompt_tokens=2000, sent={"session_id": "notebook-1"}, body=body)
    first = Step(prompt_tokens=2000, cached_tokens=900, sent={"session_id": "notebook-2"}, body=body)

    assert "session ids differ" in judge_across_cells([last], [first])[1]


def test_a_short_prompt_or_an_empty_cell_is_inconclusive():
    body = {"tools": [], "messages": []}
    short = Step(prompt_tokens=100, body=body)

    assert judge_across_cells([short], [short])[0] == "INCONCLUSIVE"
    assert judge_across_cells([], [short])[0] == "INCONCLUSIVE"


async def test_a_result_table_made_above_changes_the_system_prompt_end_and_the_check_says_why(telemetry_db, semantic_layer_path, tmp_path):
    """ADR 0007: a new Result Table changes the end of the system prompt, so the smoke check's second question must
    make none (a bare count, a SHOW Cell and Markdown notes make none)."""
    with_table = Question(id="t", question="q", sql="SELECT 1", above=[{"kind": "sql", "sql": "SELECT dport FROM flows LIMIT 3"}])
    cells, _, _ = await run_notebook("anthropic/claude-sonnet-5.5", [QUESTIONS[0], with_table],
                                     _settings(telemetry_db, semantic_layer_path, tmp_path), tmp_path / "w",
                                     inner=SimulatedProvider())

    verdict, why = judge_across_cells(*cells)
    assert verdict == "NOT WORKING" and "Result Table" in why
