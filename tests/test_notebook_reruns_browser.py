"""Drafts and re-runs in a real browser: the "edited, not run" badge and tables from earlier runs."""

import re

from conftest import ScriptedLLM, tool_call
from playwright.sync_api import expect
from test_notebooks import SQL

from flood_agent.llm import TextChunk


def open_notebook(page):
    page.get_by_role("button", name="New notebook").click()
    expect(page).to_have_url(re.compile(r"/n/\w+$"))
    return page.locator(".cellblock.ai").first


def test_editing_a_ran_cell_shows_the_badge_until_it_is_back_to_what_ran(live_page, build_agent):
    page = live_page(agent=build_agent(ScriptedLLM([TextChunk("Old answer.")])))
    cell = open_notebook(page)
    cell.get_by_label("Prompt").fill("original")
    cell.get_by_role("button", name="Run").click()
    expect(cell.locator(".reply")).to_contain_text("Old answer.")
    badge = cell.get_by_text("edited, not run")
    expect(badge).to_be_hidden()

    cell.get_by_label("Prompt").fill("original, edited")
    expect(badge).to_be_visible()
    expect(cell.locator(".reply")).to_contain_text("Old answer.")  # the old output stays
    cell.get_by_label("Prompt").fill("original")
    expect(badge).to_be_hidden()


def test_a_rerun_lists_the_earlier_tables_collapsed_and_delete_all_clears_them(live_page, build_agent):
    llm = ScriptedLLM(
        [tool_call("run_query", sql=SQL, title="First")], [TextChunk("one")],
        [tool_call("run_query", sql=SQL, title="Second")], [TextChunk("two")])
    page = live_page(agent=build_agent(llm))
    cell = open_notebook(page)
    cell.get_by_label("Prompt").fill("ports")
    cell.get_by_role("button", name="Run").click()
    expect(cell.locator(".reply")).to_contain_text("one")
    expect(cell.get_by_text("Tables from earlier runs")).to_have_count(0)
    cell.get_by_label("Prompt").fill("ports again")
    cell.get_by_role("button", name="Run").click()
    expect(cell.locator(".reply")).to_contain_text("two")

    line = cell.get_by_text("Tables from earlier runs (1)")
    expect(line).to_be_visible()
    expect(cell.get_by_role("button", name=re.compile("Delete all"))).to_be_hidden()  # collapsed
    line.click()
    expect(cell.locator(".earlier-tables .table-name .name")).to_have_count(1)
    cell.get_by_role("button", name=re.compile("Delete all")).click()
    expect(cell.get_by_text("Tables from earlier runs")).to_have_count(0)
