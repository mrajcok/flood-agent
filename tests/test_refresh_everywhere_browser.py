"""Refresh reaches a chart of the table and a Display of it in another Conversation (ADR 0003, ADR 0006)."""

from conftest import Clock, ScriptedLLM, last_tool_result, tool_call
from playwright.sync_api import expect
from test_chart_tooltip_browser import chart_script
from test_refresh_browser import HOUR, RECENT, ask_for_recent_table

from flood_agent.llm import TextChunk


def test_a_refreshed_chart_loads_the_new_rows_and_agrees_with_the_table(build_agent, live_page, monkeypatch):
    script = chart_script(RECENT, "bar", {"x": {"field": "sip", "type": "nominal"}, "y": {"field": "etime", "type": "quantitative"}})
    _, clock, page, _ = ask_for_recent_table(build_agent, live_page, monkeypatch, script)
    expect(page.locator(".display.chart canvas")).to_be_visible()
    monkeypatch.setenv("FIXED_NOW_MS", "1700000003600")
    clock.ms += HOUR

    with page.expect_response(lambda r: r.url.endswith("/chart-rows")) as reloaded:
        page.locator(".display.chart .table-name").get_by_role("button", name="Refresh").click()

    assert len(reloaded.value.json()) == 1
    expect(page.locator(".display.chart canvas")).to_be_visible()


def test_a_table_shown_from_another_conversation_is_refreshed_there_and_the_creator_sees_it(build_agent, live_page, monkeypatch):
    monkeypatch.setenv("FIXED_NOW_MS", "1700000003000")
    clock = Clock()
    llm = ScriptedLLM(
        [tool_call("run_query", sql=RECENT, title="Recent")],
        lambda msgs: [tool_call("show_table", result_table=last_tool_result(msgs)["result_table"])],
        [TextChunk("Here you go.")],
        lambda msgs: [tool_call("show_table", result_table=msgs[-1]["content"].split()[-1])],
        [TextChunk("There it is.")])
    agent = build_agent(llm, clock=clock)
    page = live_page(agent=agent)
    prompt, send = page.get_by_placeholder("Write a prompt"), page.get_by_role("button", name="Send message (Enter)")
    prompt.fill("recent flows")
    send.click()
    expect(page.locator(".reply", has_text="Here you go.")).to_be_visible()
    (table,) = agent.tables.names()
    page.get_by_role("link", name="New chat").click()
    prompt.fill(f"show {table}")
    send.click()
    expect(page.locator(".reply", has_text="There it is.")).to_be_visible()
    expect(page.locator(".display tbody tr")).to_have_count(2)

    monkeypatch.setenv("FIXED_NOW_MS", "1700000003600")
    clock.ms += HOUR
    page.locator(".display .table-name").get_by_role("button", name="Refresh").click()
    expect(page.locator(".display tbody tr")).to_have_count(1)

    page.locator("aside ul li a").last.click()  # the creator's Conversation
    expect(page.locator(".display .table-name .name")).to_have_text(table)
    expect(page.locator(".display tbody tr")).to_have_count(1)
