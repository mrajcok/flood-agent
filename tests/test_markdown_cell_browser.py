"""Markdown Cells in a real browser: edit/preview, Send to AI, and the note reaching an AI Cell."""

import re

from conftest import ScriptedLLM
from playwright.sync_api import expect

from flood_agent.llm import TextChunk


def open_notebook_with_markdown(page):
    page.get_by_role("button", name="New notebook").click()
    expect(page).to_have_url(re.compile(r"/n/\w+$"))
    page.get_by_role("button", name="+ Markdown").first.click()
    return page.locator(".cellblock.markdown")


def test_markdown_cell_edits_renders_and_double_click_edits_again(live_page):
    page = live_page()
    cell = open_notebook_with_markdown(page)

    expect(cell.get_by_label("Markdown")).to_be_visible()  # empty: opens in edit mode
    expect(cell.get_by_text("Not sent to AI Cells below")).to_be_visible()
    cell.get_by_label("Markdown").fill("## Plan\n\n**focus** on UDP")
    cell.get_by_role("button", name="Done").click()

    expect(cell.locator(".md-preview h2")).to_have_text("Plan")
    expect(cell.locator(".md-preview strong")).to_have_text("focus")
    expect(cell.get_by_label("Markdown")).to_be_hidden()

    cell.locator(".md-preview").dblclick()
    expect(cell.get_by_label("Markdown")).to_be_visible()
    expect(cell.get_by_label("Markdown")).to_have_value("## Plan\n\n**focus** on UDP")


def test_send_to_ai_checkbox_hides_the_not_sent_note_and_survives_a_reload(live_page):
    page = live_page()
    cell = open_notebook_with_markdown(page)
    cell.get_by_label("Markdown").fill("note")
    cell.get_by_label("Send to AI").check()
    expect(cell.get_by_text("Not sent to AI Cells below")).to_be_hidden()
    cell.get_by_role("button", name="Done").click()

    page.reload()
    cell = page.locator(".cellblock.markdown")
    expect(cell.get_by_label("Send to AI")).to_be_checked()
    expect(cell.get_by_text("Not sent to AI Cells below")).to_be_hidden()
    cell.get_by_label("Send to AI").uncheck()
    page.reload()
    expect(page.locator(".cellblock.markdown").get_by_text("Not sent to AI Cells below")).to_be_visible()


def test_a_ticked_note_above_an_ai_cell_is_sent_with_the_run(live_page, build_agent):
    llm = ScriptedLLM([TextChunk("Noted.")])
    page = live_page(agent=build_agent(llm))
    page.get_by_role("button", name="New notebook").click()
    expect(page).to_have_url(re.compile(r"/n/\w+$"))
    # a Markdown Cell after the first AI Cell, then a second AI Cell below it
    page.get_by_role("button", name="+ Markdown").first.click()
    note = page.locator(".cellblock.markdown")
    note.get_by_label("Markdown").fill("Focus on UDP")
    note.get_by_label("Send to AI").check()
    note.get_by_role("button", name="Done").click()
    page.get_by_role("button", name="+ AI").last.click()
    second = page.locator(".cellblock.ai").nth(1)
    second.get_by_label("Prompt").fill("what now")
    second.get_by_role("button", name="Run").click()
    expect(second.locator(".reply")).to_contain_text("Noted.")
    assert "[Markdown note] Focus on UDP" in str(llm.calls[0])
