"""The stale dot in a real browser: it appears when a cell above changes what it is sent, and a re-run clears it."""

import re

from conftest import ScriptedLLM
from playwright.sync_api import expect

from flood_agent.llm import TextChunk


def test_the_stale_dot_follows_what_cells_above_send_and_a_rerun_clears_it(live_page, build_agent):
    page = live_page(agent=build_agent(ScriptedLLM([TextChunk("One.")], [TextChunk("Two.")])))
    page.get_by_role("button", name="New notebook").click()
    expect(page).to_have_url(re.compile(r"/n/\w+$"))
    page.locator(".nb-head").get_by_role("button", name="+ Markdown").first.click()
    note = page.locator(".cellblock.markdown")
    note.get_by_label("Markdown").fill("Focus on UDP")
    note.get_by_role("button", name="Done").click()
    page.locator(".nb-head").get_by_role("button", name="+ AI").last.click()
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


def test_the_stale_dot_sits_just_above_and_right_of_the_run_button_in_a_narrow_gutter(live_page, build_agent):
    page = live_page(agent=build_agent(ScriptedLLM([TextChunk("One.")])))
    page.get_by_role("button", name="New notebook").click()
    expect(page).to_have_url(re.compile(r"/n/\w+$"))
    page.locator(".nb-head").get_by_role("button", name="+ Markdown").click()
    note = page.locator(".cellblock.markdown")
    note.get_by_label("Markdown").fill("Focus on UDP")
    note.get_by_role("button", name="Done").click()
    page.locator(".nb-head").get_by_role("button", name="+ AI").click()
    cell = page.locator(".cellblock.ai").nth(1)
    cell.get_by_label("Prompt").fill("ask")
    cell.get_by_role("button", name="Run").click()
    expect(cell.locator(".reply")).to_contain_text("One.")
    note.get_by_label("Send to AI").check()
    dot = cell.locator(".stale-dot")
    expect(dot).to_be_visible()
    run, d, gutter = (cell.locator(s).bounding_box() for s in (".gbtn.run", ".stale-dot", ".gutter"))
    assert run and d and gutter
    assert d["x"] + d["width"] / 2 > run["x"] + run["width"] / 2  # to the right of the button's centre
    assert d["y"] + d["height"] / 2 < run["y"] + run["height"] / 2  # above its centre
    assert abs(d["x"] - (run["x"] + run["width"])) < 12 and abs(d["y"] - run["y"]) < 12  # very close to it
    assert gutter["width"] < 40  # no room reserved for the dot
    aside, main = page.locator("aside").bounding_box(), page.locator("main").bounding_box()
    assert aside and main
    assert main["x"] - (aside["x"] + aside["width"]) == 8  # .layout gap is .5rem
