"""The AI Cell's enter arrow in the prompt box: Send, then a Stop with a turning ring; Esc or Ctrl-C also stop."""

import re

from conftest import ScriptedLLM
from playwright.sync_api import expect
from test_notebook_browser import open_new_notebook, write_prompt
from test_stop_turn import HangingLLM

from flood_agent.llm import TextChunk


def test_the_enter_arrow_sends_the_prompt_and_is_off_until_there_is_one(live_page, build_agent):
    page = live_page(agent=build_agent(ScriptedLLM([TextChunk("Sent.")])))
    cell = open_new_notebook(page)
    send = cell.get_by_role("button", name="Send", exact=True)
    expect(send).to_be_visible()
    expect(send).to_be_disabled()
    write_prompt(cell, "hello")
    expect(send).to_be_enabled()
    send.click()
    expect(cell.locator(".reply")).to_contain_text("Sent.")
    expect(cell.get_by_role("button", name="Interrupt")).to_be_hidden()


def test_while_running_the_arrow_is_a_stop_with_a_turning_ring_that_stops_the_cell(live_page, build_agent):
    page = live_page(agent=build_agent(HangingLLM()))
    cell = open_new_notebook(page)
    write_prompt(cell, "slow one")
    cell.get_by_role("button", name="Send", exact=True).click()
    stop = cell.get_by_role("button", name="Interrupt")
    expect(stop).to_be_visible()
    expect(cell.get_by_role("button", name="Send", exact=True)).to_be_hidden()
    expect(cell.locator(".prompt-box .send.stop circle")).to_have_css("animation-name", "ring-turn")
    expect(cell.locator(".output")).to_contain_text("Partial answer")
    stop.click()
    expect(cell.locator(".output .cell-stopped")).to_have_text("stopped by the user")
    expect(stop).to_be_hidden()
    expect(cell.get_by_role("button", name="Send", exact=True)).to_be_visible()


def test_escape_stops_a_running_cell(live_page, build_agent):
    page = live_page(agent=build_agent(HangingLLM()))
    cell = open_new_notebook(page)
    write_prompt(cell, "slow one")
    cell.get_by_role("button", name="Send", exact=True).click()
    expect(cell.locator(".output")).to_contain_text("Partial answer")
    page.keyboard.press("Escape")
    expect(cell.locator(".output .cell-stopped")).to_have_text("stopped by the user")


def test_ctrl_c_stops_a_running_cell_only_when_nothing_is_selected(live_page, build_agent):
    page = live_page(agent=build_agent(HangingLLM()))
    cell = open_new_notebook(page)
    write_prompt(cell, "slow one")
    cell.get_by_role("button", name="Send", exact=True).click()
    expect(cell.locator(".output")).to_contain_text("Partial answer")
    cell.get_by_label("Prompt").evaluate("e => { e.focus(); e.select(); }")  # copying the prompt must not stop the run
    page.keyboard.press("Control+c")
    page.wait_for_timeout(500)
    expect(cell.locator(".cell-stopped")).to_have_count(0)
    page.mouse.click(5, 5)  # nothing selected now
    page.keyboard.press("Control+c")
    expect(cell.locator(".output .cell-stopped")).to_have_text("stopped by the user")
    assert re.search(r"stopped", cell.locator(".cell-foot").inner_text())
