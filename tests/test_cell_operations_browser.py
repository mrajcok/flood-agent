"""Cell operations in a real browser: add, move, copy and delete, with stale dots following."""

import re

from conftest import ScriptedLLM, add_below
from playwright.sync_api import expect

from flood_agent.llm import TextChunk


def operate(cell, label):
    """Hover the cell to bring up its toolbar, then press one of its buttons."""
    cell.hover()
    cell.get_by_role("button", name=label).click()


def test_add_move_copy_and_delete_cells_and_the_stale_dot_follows(live_page, build_agent):
    page = live_page(agent=build_agent(ScriptedLLM([TextChunk("One.")])))
    page.get_by_role("button", name="New notebook").click()
    expect(page).to_have_url(re.compile(r"/n/\w+$"))
    cells = page.locator(".cellblock")
    ai = page.locator(".cellblock.ai")
    ai.get_by_label("Prompt").fill("ask")
    ai.get_by_role("button", name="Run").click()
    expect(ai.locator(".reply")).to_contain_text("One.")

    # Add a Markdown Cell below the AI Cell (the title line's button); sending it to the AI Cell below does nothing yet.
    add_below(page, "markdown")
    expect(cells).to_have_count(2)
    note = page.locator(".cellblock.markdown")
    expect(cells.nth(1)).to_have_attribute("data-kind", "markdown")
    note.get_by_label("Markdown").fill("Focus on UDP")
    note.get_by_role("button", name="Done").click()
    note.get_by_label("Send to AI").check()
    page.wait_for_load_state("networkidle")  # the tick is saved before the note moves
    expect(ai.locator(".stale-dot")).to_be_hidden()

    # Moved above the AI Cell the note is an input to it, so it goes stale; back down it is not.
    operate(note, "Move cell up")
    expect(cells.first).to_have_attribute("data-kind", "markdown")
    expect(ai.locator(".stale-dot")).to_be_visible()
    operate(note, "Move cell down")
    expect(cells.first).to_have_attribute("data-kind", "ai")
    expect(ai.locator(".stale-dot")).to_be_hidden()
    operate(note, "Move cell up")
    expect(ai.locator(".stale-dot")).to_be_visible()

    # Copy the AI Cell: same prompt, no output.
    operate(ai, "Copy cell")
    expect(cells).to_have_count(3)
    copy = cells.nth(2)
    expect(copy.get_by_label("Prompt")).to_have_value("ask")
    expect(copy.locator(".reply")).to_have_count(0)

    # Delete the note through the dialog: the stale dot goes.
    operate(note, "Delete cell")
    expect(page.get_by_role("dialog").locator("h3")).to_have_text("Delete this Markdown cell?")
    page.get_by_role("dialog").get_by_role("button", name="Delete", exact=True).click()
    expect(cells).to_have_count(2)
    expect(ai.first.locator(".stale-dot")).to_be_hidden()
    page.reload()
    expect(page.locator(".cellblock")).to_have_count(2)


def test_the_insert_strip_adds_below_its_cell_and_the_title_line_stays_put(live_page):
    page = live_page()
    page.get_by_role("button", name="New notebook").click()
    expect(page).to_have_url(re.compile(r"/n/\w+$"))
    cells = page.locator(".cellblock")
    add_below(page, "sql")
    expect(cells).to_have_count(2)
    add_below(page, "ai")  # below the last cell, the new SQL Cell
    expect(cells).to_have_count(3)
    assert [c.get_attribute("data-kind") for c in cells.all()] == ["ai", "sql", "ai"]
    add_below(page, "markdown", cells.first)
    expect(cells).to_have_count(4)
    assert [c.get_attribute("data-kind") for c in cells.all()] == ["ai", "markdown", "sql", "ai"]
    head = page.locator(".nb-head").bounding_box()
    for _ in range(6):
        add_below(page, "ai")
    page.locator("#prompt-area").evaluate("e => e.scrollTop = e.scrollHeight")
    assert page.locator(".nb-head").bounding_box() == head


def test_the_cell_toolbar_shows_on_hover_on_every_cell_even_a_rendered_markdown_one(live_page):
    page = live_page()
    page.get_by_role("button", name="New notebook").click()
    expect(page).to_have_url(re.compile(r"/n/\w+$"))
    ai = page.locator(".cellblock.ai")
    toolbar = ai.get_by_role("group", name="Cell operations")
    expect(toolbar).to_be_hidden()
    ai.hover()
    expect(toolbar).to_be_visible()
    expect(toolbar).to_have_css("opacity", re.compile(r"^0\.5"))  # faint until the pointer is on the toolbar itself
    toolbar.hover()
    expect(toolbar).to_have_css("opacity", "1")
    add_below(page, "markdown")
    note = page.locator(".cellblock.markdown")
    note.get_by_label("Markdown").fill("hello")
    note.hover()
    expect(note.get_by_role("group", name="Cell operations")).to_be_visible()
    note.get_by_role("button", name="Done").click()
    note.hover()
    expect(note.get_by_role("group", name="Cell operations")).to_be_visible()


def test_hovering_the_gap_below_a_cell_offers_inserts_that_land_between_cells(live_page):
    page = live_page()
    page.get_by_role("button", name="New notebook").click()
    expect(page).to_have_url(re.compile(r"/n/\w+$"))
    cells = page.locator(".cellblock")
    add_below(page, "markdown")
    first = cells.first
    insert = first.get_by_role("group", name="Insert a cell below")
    page.mouse.move(2, 2)  # away from the strip the add above left the pointer on...
    page.evaluate("document.activeElement.blur()")  # ...and the focus in
    expect(insert.get_by_role("button", name="+ SQL")).to_be_hidden()
    box = first.locator(".cell").bounding_box()
    assert box
    page.mouse.move(box["x"] + box["width"] / 2, box["y"] + box["height"] + 2)  # in the gap under the first cell
    expect(insert.get_by_role("button", name="+ SQL")).to_be_visible()
    insert.get_by_role("button", name="+ SQL").click()
    expect(cells).to_have_count(3)
    assert [c.get_attribute("data-kind") for c in cells.all()] == ["ai", "sql", "markdown"]
    expect(cells.nth(1).locator("textarea").first).to_be_focused()  # the new cell is ready to type in


def test_a_notebook_with_no_cells_offers_the_three_kinds_in_the_cell_area(live_page):
    page = live_page()
    page.get_by_role("button", name="New notebook").click()
    expect(page).to_have_url(re.compile(r"/n/\w+$"))
    empty = page.locator(".cells-empty")
    expect(empty).to_be_hidden()
    expect(page.locator(".nb-head").get_by_role("button")).to_have_count(0)  # nothing on the title line
    operate(page.locator(".cellblock"), "Delete cell")
    page.get_by_role("dialog").get_by_role("button", name="Delete", exact=True).click()
    expect(page.locator(".cellblock")).to_have_count(0)
    expect(empty).to_contain_text("Add a cell:")
    for kind in ("+ AI", "+ SQL", "+ Markdown"):
        expect(empty.get_by_role("button", name=kind)).to_be_visible()
    empty.get_by_role("button", name="+ SQL").click()
    expect(page.locator(".cellblock.sql")).to_have_count(1)
    expect(empty).to_be_hidden()
    page.reload()
    expect(empty).to_be_hidden()
