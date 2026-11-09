"""The Refresh control on a table Display's name line: where it shows, its busy state, its error, the
in-place update and the "as of" text (ADR 0006). The endpoint itself is tested in test_refresh_table.py."""

import sqlite3
import threading
from contextlib import closing

from conftest import Clock, ScriptedLLM, last_tool_result, tool_call
from playwright.sync_api import expect
from test_chart_tooltip_browser import chart_script

from flood_agent import queries
from flood_agent.llm import TextChunk

RECENT = "SELECT sip, etime FROM flows WHERE etime > now() - 1500 ORDER BY etime"
HOUR = 3_600_000


def ask_for_recent_table(build_agent, live_page, monkeypatch, script=None):
    monkeypatch.setenv("FIXED_NOW_MS", "1700000003000")
    clock = Clock()
    llm = ScriptedLLM(*(script or [
        [tool_call("run_query", sql=RECENT, title="Recent")],
        lambda msgs: [tool_call("show_table", result_table=last_tool_result(msgs)["result_table"])],
        [TextChunk("Here you go.")],
    ]))
    agent = build_agent(llm, clock=clock)
    page = live_page(agent=agent)
    page.get_by_placeholder("Write a prompt").fill("recent flows")
    page.get_by_role("button", name="Send message (Enter)").click()
    expect(page.locator(".reply", has_text=("Here you go." if script is None else "Here is the chart."))).to_be_visible()
    (table,) = agent.tables.names()
    return agent, clock, page, table


def test_refresh_updates_the_table_in_place_and_moves_its_as_of_time(build_agent, live_page, monkeypatch):
    agent, clock, page, _ = ask_for_recent_table(build_agent, live_page, monkeypatch)
    line = page.locator(".display .table-name")
    expect(page.locator(".display tbody tr")).to_have_count(2)
    first_as_of = line.locator(".as-of").inner_text()
    assert first_as_of.startswith("as of ")

    monkeypatch.setenv("FIXED_NOW_MS", "1700000003600")
    clock.ms += HOUR
    line.get_by_role("button", name="Refresh").click()

    expect(page.locator(".display tbody tr")).to_have_count(1)
    expect(page.get_by_text("(1 rows)")).to_be_visible()
    assert line.locator(".as-of").inner_text() != first_as_of
    expect(line.locator(".expires")).to_have_text("expires in 1d")
    expect(line.get_by_role("button", name="Refresh")).to_be_enabled()


def test_the_control_is_disabled_while_a_refresh_runs(build_agent, live_page, monkeypatch):
    agent, _, page, _ = ask_for_recent_table(build_agent, live_page, monkeypatch)
    started, release, real = threading.Event(), threading.Event(), queries.run_query

    def slow(*args, **kwargs):
        started.set()
        release.wait(5)
        return real(*args, **kwargs)

    monkeypatch.setattr(queries, "run_query", slow)
    button = page.locator(".display .table-name").get_by_role("button", name="Refresh")
    button.click()
    assert started.wait(5)
    expect(button).to_be_disabled()
    release.set()
    expect(button).to_be_enabled()


def test_a_failed_refresh_shows_a_plain_error_and_keeps_the_table(build_agent, live_page, monkeypatch):
    agent, _, page, table = ask_for_recent_table(build_agent, live_page, monkeypatch)
    with closing(sqlite3.connect(agent.tables.path)) as con, con:
        con.execute("UPDATE result_tables SET sql='DROP TABLE flows'")
    line = page.locator(".display .table-name")

    line.get_by_role("button", name="Refresh").click()

    expect(line.locator(".refresh-error")).to_contain_text("Only a single read-only SELECT")
    expect(page.locator(".display tbody tr")).to_have_count(2)
    expect(line.get_by_role("button", name="Refresh")).to_be_enabled()


def test_a_table_with_no_stored_query_has_no_refresh_control(build_agent, live_page, monkeypatch):
    agent, _, page, table = ask_for_recent_table(build_agent, live_page, monkeypatch)
    with closing(sqlite3.connect(agent.tables.path)) as con, con:
        con.execute("UPDATE result_tables SET sql=NULL, max_rows=NULL")
    page.reload()
    page.locator("aside ul li a").first.click()
    expect(page.locator(".display tbody tr")).to_have_count(2)
    expect(page.locator(".display .table-name .name")).to_have_text(table)
    expect(page.get_by_role("button", name="Refresh")).to_have_count(0)
    expect(page.locator(".display .table-name .as-of")).to_be_hidden()


def test_a_chart_reloads_its_rows_when_its_table_is_refreshed(build_agent, live_page, monkeypatch):
    script = chart_script(RECENT, "bar", {"x": {"field": "sip", "type": "nominal"}, "y": {"field": "etime", "type": "quantitative"}})
    _, _, page, _ = ask_for_recent_table(build_agent, live_page, monkeypatch, script)
    expect(page.locator(".display.chart canvas")).to_be_visible()
    button = page.locator(".display.chart .table-name").get_by_role("button", name="Refresh")

    with page.expect_request(lambda r: r.url.endswith("/chart-rows")):
        button.click()

    expect(page.locator(".display.chart canvas")).to_be_visible()


def test_the_table_info_line_hides_the_refresh_control_with_the_rest(build_agent, live_page, monkeypatch):
    _, _, page, _ = ask_for_recent_table(build_agent, live_page, monkeypatch)
    expect(page.get_by_role("button", name="Refresh")).to_be_visible()
    page.evaluate("document.body.classList.add('hide-tables')")
    expect(page.get_by_role("button", name="Refresh")).to_be_hidden()


def test_the_truncated_notice_follows_a_refresh(build_agent, live_page, monkeypatch):
    agent, _, page, _ = ask_for_recent_table(build_agent, live_page, monkeypatch)
    badge = page.locator(".display .badge-truncated")
    expect(page.locator(".display tbody tr")).to_have_count(2)
    expect(badge).to_be_hidden()
    with closing(sqlite3.connect(agent.tables.path)) as con, con:
        con.execute("UPDATE result_tables SET max_rows=1")  # the next run is cut to one row

    page.locator(".display .table-name").get_by_role("button", name="Refresh").click()

    expect(page.locator(".display tbody tr")).to_have_count(1)
    expect(badge).to_be_visible()
    expect(badge).to_have_text("truncated to 1")
