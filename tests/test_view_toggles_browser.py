"""The Traces / Live traces / Table info sliders along the top, driven in headless system Chrome."""

import re

from conftest import ScriptedLLM
from playwright.sync_api import expect
from test_conversations import table_script


def ask_for_a_table(page):
    page.get_by_placeholder("Write a prompt").fill("show me flows")
    page.get_by_role("button", name="Send message (Enter)").click()
    expect(page.locator(".reply", has_text="Here you go.")).to_be_visible()


def test_the_sliders_start_on_and_traces_off_hides_every_trace_and_greys_live_traces(build_agent, live_page):
    page = live_page(agent=build_agent(ScriptedLLM(*table_script())))
    ask_for_a_table(page)
    traces, live, names = (page.get_by_label(n, exact=True) for n in ("Traces", "Live traces", "Table info"))
    for s in (traces, live, names):
        expect(s).to_be_checked()
    expect(page.locator("details.trace").first).to_be_visible()
    traces.uncheck()
    expect(page.locator("details.trace").first).to_be_hidden()
    expect(live).to_be_disabled()
    traces.check()
    expect(page.locator("details.trace").first).to_be_visible()
    expect(live).to_be_enabled()


def test_table_info_off_hides_the_name_line_but_not_the_table(build_agent, live_page):
    page = live_page(agent=build_agent(ScriptedLLM(*table_script())))
    ask_for_a_table(page)
    page.get_by_label("Table info").uncheck()
    expect(page.locator(".display table").first).to_be_visible()
    expect(page.locator(".table-name").first).to_be_hidden()


def test_a_table_names_choice_saved_before_the_rename_still_hides_the_line(build_agent, live_page):
    page = live_page(agent=build_agent(ScriptedLLM(*table_script())))
    page.evaluate("localStorage.setItem('viewToggles', JSON.stringify({tables: false}))")
    page.reload()
    ask_for_a_table(page)
    expect(page.get_by_label("Table info")).not_to_be_checked()
    expect(page.locator(".table-name").first).to_be_hidden()


def test_live_traces_off_hides_the_live_trace_of_a_running_turn(live_page):
    page = live_page()
    page.get_by_label("Live traces").uncheck()
    assert page.evaluate("document.body.classList.contains('hide-live')")
    page.get_by_label("Live traces").check()
    assert not page.evaluate("document.body.classList.contains('hide-live')")


def test_the_choices_survive_a_reload(live_page):
    page = live_page()
    page.get_by_label("Table info").uncheck()
    page.reload()
    expect(page.get_by_label("Table info")).not_to_be_checked()
    assert page.evaluate("document.body.classList.contains('hide-tables')")


def test_collapse_turns_is_still_a_button(live_page):
    page = live_page()
    expect(page.get_by_role("button", name=re.compile("(Collapse|Expand) turns"))).to_be_visible()
