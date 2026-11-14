"""The stale dot in a real browser: it appears when a cell above changes what it is sent, and a re-run clears it."""

import re

from conftest import ScriptedLLM
from playwright.sync_api import expect

from flood_agent.llm import TextChunk


def test_the_stale_dot_follows_what_cells_above_send_and_a_rerun_clears_it(live_page, build_agent):
    page = live_page(agent=build_agent(ScriptedLLM([TextChunk("One.")], [TextChunk("Two.")])))
    page.get_by_role("button", name="New notebook").click()
    expect(page).to_have_url(re.compile(r"/n/\w+$"))
    page.get_by_role("button", name="+ Markdown").first.click()
    note = page.locator(".cellblock.markdown")
    note.get_by_label("Markdown").fill("Focus on UDP")
    note.get_by_role("button", name="Done").click()
    page.get_by_role("button", name="+ AI").last.click()
    cell = page.locator(".cellblock.ai").nth(1)
    cell.get_by_label("Prompt").fill("ask")
    cell.get_by_role("button", name="Run").click()
    expect(cell.locator(".reply")).to_contain_text("One.")
    dot = cell.locator(".stale-dot")
    expect(dot).to_be_hidden()

    note.get_by_label("Send to AI").check()  # the note now reaches the cell below
    expect(dot).to_be_visible()
    expect(dot).to_have_attribute("title", "Cells above changed since this ran")
    note.get_by_label("Send to AI").uncheck()  # and back to what ran
    expect(dot).to_be_hidden()
    note.get_by_label("Send to AI").check()
    expect(dot).to_be_visible()

    cell.get_by_role("button", name="Run").click()
    expect(cell.locator(".reply")).to_contain_text("Two.")
    expect(dot).to_be_hidden()
