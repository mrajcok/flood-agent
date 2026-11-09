"""A Count Display renders as a one-line item (title, number, "as of") where the call happened, and is
still there after a reload (ADR 0006). The saving itself is tested in test_count_display.py."""

import re

from conftest import ScriptedLLM, tool_call
from playwright.sync_api import expect

from flood_agent.llm import TextChunk


def test_count_display_shows_title_number_and_as_of_in_reply_order_and_survives_reload(build_agent, live_page):
    llm = ScriptedLLM(
        [TextChunk("Counting."), tool_call("run_count_query", sql="SELECT COUNT(*) FROM flows", title="All flows")],
        [TextChunk("Done counting.")],
    )
    page = live_page(agent=build_agent(llm))
    page.get_by_placeholder("Write a prompt").fill("how many flows?")
    page.get_by_role("button", name="Send message (Enter)").click()
    reply = page.locator(".reply")
    expect(reply.get_by_text("Done counting.")).to_be_visible()

    line = reply.locator(".count-display")
    expect(line).to_have_count(1)
    expect(line.locator(".count-title")).to_have_text("All flows")
    expect(line.locator(".count-value")).to_have_text("3")
    expect(line.locator(".as-of")).to_have_text(re.compile(r"^as of \d"))
    text = reply.inner_text()
    assert text.index("Counting.") < text.index("All flows") < text.index("Done counting.")

    page.goto(page.locator("aside a[href^=\"/c/\"]").first.get_attribute("href"))
    page.locator(".count-display").wait_for()
    expect(page.locator(".count-display .count-value")).to_have_text("3")
