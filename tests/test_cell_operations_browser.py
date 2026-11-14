"""Cell operations in a real browser: insert above, move, copy and delete, with stale dots following."""

import re

from conftest import ScriptedLLM
from playwright.sync_api import expect

from flood_agent.llm import TextChunk


def test_insert_move_copy_and_delete_cells_and_the_stale_dot_follows(live_page, build_agent):
    page = live_page(agent=build_agent(ScriptedLLM([TextChunk("One.")])))
    page.get_by_role("button", name="New notebook").click()
    expect(page).to_have_url(re.compile(r"/n/\w+$"))
    cells = page.locator(".cellblock")
    ai = page.locator(".cellblock.ai")
    ai.get_by_label("Prompt").fill("ask")
    ai.get_by_role("button", name="Run").click()
    expect(ai.locator(".reply")).to_contain_text("One.")

    # Insert above the first cell; ticking it sends it to the AI Cell below, which goes stale.
    page.get_by_role("button", name="Insert Markdown Cell above the first cell").click()
    expect(cells).to_have_count(2)
    note = page.locator(".cellblock.markdown")
    expect(cells.first).to_have_attribute("data-kind", "markdown")
    note.get_by_label("Markdown").fill("Focus on UDP")
    note.get_by_role("button", name="Done").click()
    note.get_by_label("Send to AI").check()
    expect(ai.locator(".stale-dot")).to_be_visible()

    # Moved below the AI Cell the note is no longer above it, so it is not stale; back up it is.
    page.get_by_role("button", name="Move cell down").first.click()
    expect(cells.first).to_have_attribute("data-kind", "ai")
    expect(ai.locator(".stale-dot")).to_be_hidden()
    page.get_by_role("button", name="Move cell up").last.click()
    expect(cells.first).to_have_attribute("data-kind", "markdown")
    expect(ai.locator(".stale-dot")).to_be_visible()

    # Copy the AI Cell: same prompt, no output.
    page.get_by_role("button", name="Copy cell").last.click()
    expect(cells).to_have_count(3)
    copy = cells.nth(2)
    expect(copy.get_by_label("Prompt")).to_have_value("ask")
    expect(copy.locator(".reply")).to_have_count(0)

    # Delete the note through the dialog: the stale dot goes.
    page.get_by_role("button", name="Delete cell").first.click()
    page.get_by_role("dialog").get_by_role("button", name="Delete", exact=True).click()
    expect(cells).to_have_count(2)
    expect(ai.first.locator(".stale-dot")).to_be_hidden()
    page.reload()
    expect(page.locator(".cellblock")).to_have_count(2)
