"""Esc closes the Tables and Explain dialogs."""

from conftest import ScriptedLLM
from playwright.sync_api import expect
from test_explain_browser import trace_with
from test_stop_button_browser import send
from test_stop_turn import HangingLLM


def test_escape_closes_the_tables_dialog(live_page, build_agent):
    page = live_page(agent=build_agent(ScriptedLLM()))
    page.get_by_role("button", name="Tables").click()
    dialog = page.get_by_role("dialog", name="Tables you can query")
    expect(dialog).to_be_visible()
    page.keyboard.press("Escape")
    expect(dialog).to_be_hidden()


def test_escape_closes_the_explain_dialog(live_page, build_agent):
    page, _ = trace_with(live_page, build_agent, "SELECT dport FROM flows")
    page.locator("details.trace .sql-label").get_by_role("button", name="Explain").click()
    dialog = page.locator("dialog.explain")
    expect(dialog.locator(".plan")).to_be_visible()
    page.keyboard.press("Escape")
    expect(dialog).to_be_hidden()


def test_escape_in_a_dialog_while_a_turn_runs_closes_it_without_stopping_the_turn(live_page, build_agent):
    page = live_page(agent=build_agent(HangingLLM()))
    send(page)
    stop = page.get_by_role("button", name="Stop")
    expect(stop).to_be_visible()
    page.get_by_role("button", name="Tables").click()
    dialog = page.get_by_role("dialog", name="Tables you can query")
    expect(dialog).to_be_visible()
    page.keyboard.press("Escape")
    expect(dialog).to_be_hidden()
    page.wait_for_timeout(2000)  # long enough for a stop to end the turn
    expect(stop).to_be_visible()
