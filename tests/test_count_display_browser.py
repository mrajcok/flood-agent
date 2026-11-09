"""A Count Display renders as a one-line item (title, number, "as of") where the call happened, and is
still there after a reload (ADR 0006). The saving itself is tested in test_count_display.py."""

import re
import sqlite3
import threading
from contextlib import closing

from conftest import Clock, ScriptedLLM, tool_call
from playwright.sync_api import expect

from flood_agent import queries
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

    page.goto(page.url.rstrip("/").split("/c/")[0] + page.locator("aside a[href^=\"/c/\"]").first.get_attribute("href"))
    page.locator(".count-display").wait_for()
    expect(page.locator(".count-display .count-value")).to_have_text("3")


RECENT_COUNT = "SELECT COUNT(*) FROM flows WHERE etime > now() - 1500"  # 2 at 1700000003000, 1 at ...3600


def ask_for_recent_count(build_agent, live_page, monkeypatch):
    monkeypatch.setenv("FIXED_NOW_MS", "1700000003000")
    clock = Clock()
    llm = ScriptedLLM([tool_call("run_count_query", sql=RECENT_COUNT, title="Recent")], [TextChunk("Counted.")])
    agent = build_agent(llm, clock=clock)
    page = live_page(agent=agent)
    page.get_by_placeholder("Write a prompt").fill("recent flows")
    page.get_by_role("button", name="Send message (Enter)").click()
    expect(page.locator(".reply", has_text="Counted.")).to_be_visible()
    return agent, clock, page


def test_refresh_updates_the_count_in_place_and_moves_its_as_of_time(build_agent, live_page, monkeypatch):
    _, clock, page = ask_for_recent_count(build_agent, live_page, monkeypatch)
    line = page.locator(".count-display")
    expect(line.locator(".count-value")).to_have_text("2")
    first_as_of = line.locator(".as-of").inner_text()

    monkeypatch.setenv("FIXED_NOW_MS", "1700000003600")
    clock.ms += 3_600_000
    line.get_by_role("button", name="Refresh").click()

    expect(line.locator(".count-value")).to_have_text("1")
    assert line.locator(".as-of").inner_text() != first_as_of
    expect(line.get_by_role("button", name="Refresh")).to_be_enabled()


def test_the_count_control_is_disabled_while_a_refresh_runs(build_agent, live_page, monkeypatch):
    _, _, page = ask_for_recent_count(build_agent, live_page, monkeypatch)
    started, release, real = threading.Event(), threading.Event(), queries.run_count_query

    def slow(*args, **kwargs):
        started.set()
        release.wait(5)
        return real(*args, **kwargs)

    monkeypatch.setattr(queries, "run_count_query", slow)
    button = page.locator(".count-display").get_by_role("button", name="Refresh")
    button.click()
    assert started.wait(5)
    expect(button).to_be_disabled()
    release.set()
    expect(button).to_be_enabled()


def test_a_failed_count_refresh_keeps_the_number_and_shows_a_plain_error(build_agent, live_page, monkeypatch):
    agent, _, page = ask_for_recent_count(build_agent, live_page, monkeypatch)
    with closing(sqlite3.connect(agent.store.path)) as con, con:
        con.execute("UPDATE displays SET definition=json_set(definition, '$.sql', 'DROP TABLE flows')")
    line = page.locator(".count-display")

    line.get_by_role("button", name="Refresh").click()

    expect(line.locator(".refresh-error")).to_contain_text("Only a single read-only SELECT")
    expect(line.locator(".count-value")).to_have_text("2")
    expect(line.get_by_role("button", name="Refresh")).to_be_enabled()


def test_a_reopened_conversation_shows_the_refresh_control_on_its_count(build_agent, live_page, monkeypatch):
    _, _, page = ask_for_recent_count(build_agent, live_page, monkeypatch)
    page.goto(page.url.rstrip("/").split("/c/")[0] + page.locator("aside a[href^=\"/c/\"]").first.get_attribute("href"))
    expect(page.locator(".count-display").get_by_role("button", name="Refresh")).to_be_visible()
