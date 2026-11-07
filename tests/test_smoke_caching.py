"""The caching smoke check's verdict logic (the script itself calls the real API and is run by hand)."""

from flood_agent.evalset.smoke_caching import Step, judge, prefix_difference

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
