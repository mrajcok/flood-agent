"""Notebook management in a real browser: rename, delete, search, hiding the sidebar and the view toggles."""

import re

from conftest import ScriptedLLM, tool_call
from playwright.sync_api import expect
from test_notebook_browser import open_new_notebook, run, write_prompt
from test_conversations import table_script
from test_notebooks import SQL

from flood_agent.llm import TextChunk


def row(page, title):
    return page.locator(".notebook-list li", has_text=title)


def test_rename_a_notebook_from_its_menu(live_page):
    page = live_page()
    open_new_notebook(page)
    row(page, "Notebook 1").get_by_role("button", name="Notebook options").click()
    page.get_by_role("menuitem", name="Rename").click()
    box = page.get_by_label("Notebook name")
    box.fill("Port scans")
    box.press("Enter")
    expect(page).to_have_url(re.compile(r"/n/\w+$"))
    expect(page.locator(".notebook-list li.current a")).to_have_text("Port scans")
    expect(page.locator("h2.nb-title")).to_have_text("Port scans")


def test_delete_a_notebook_through_the_choice_dialog(live_page):
    page = live_page()
    open_new_notebook(page)
    page.get_by_role("button", name="New notebook").click()
    expect(page.locator(".notebook-list li")).to_have_count(2)
    row(page, "Notebook 2").get_by_role("button", name="Notebook options").click()
    page.get_by_role("menuitem", name="Delete").click()
    dialog = page.get_by_role("dialog")
    expect(dialog).to_contain_text("Delete this notebook?")
    dialog.get_by_role("button", name="Cancel").click()
    expect(page.locator(".notebook-list li")).to_have_count(2)
    row(page, "Notebook 2").get_by_role("button", name="Notebook options").click()
    page.get_by_role("menuitem", name="Delete").click()
    page.get_by_role("dialog").get_by_role("button", name="Delete", exact=True).click()
    expect(page.locator(".notebook-list li")).to_have_count(1)
    expect(page.locator(".notebook-list li")).to_have_text(re.compile("Notebook 1"))


def test_search_filters_as_you_type_by_title_and_cell_text(live_page):
    page = live_page()
    cell = open_new_notebook(page)
    write_prompt(cell, "why is circuit A1B2 slow?")
    cell.get_by_label("Prompt").blur()
    page.wait_for_timeout(300)  # the draft's save lands before the search asks
    page.get_by_role("button", name="New notebook").click()
    expect(page.locator(".notebook-list li")).to_have_count(2)
    search = page.get_by_label("Search notebooks")
    search.fill("a1b2")
    expect(page.locator(".notebook-list li:visible")).to_have_count(1)
    expect(page.locator(".notebook-list li:visible")).to_have_text(re.compile("Notebook 1"))
    search.fill("zzz")
    expect(page.locator(".notebook-list li:visible")).to_have_count(0)
    expect(page.get_by_text("No notebook matches.")).to_be_visible()
    search.fill("notebook 2")
    expect(page.locator(".notebook-list li:visible")).to_have_count(1)
    search.fill("")
    expect(page.locator(".notebook-list li:visible")).to_have_count(2)


def test_hiding_the_sidebar_widens_the_cells_and_survives_a_reload(live_page):
    page = live_page()
    open_new_notebook(page)
    cells = page.locator("#cells")
    before = cells.bounding_box()["width"]
    page.get_by_role("button", name="Hide sidebar").click()
    expect(page.get_by_role("button", name="Show sidebar")).to_be_visible()
    expect(page.get_by_label("Search notebooks")).to_be_hidden()
    assert cells.bounding_box()["width"] > before
    page.reload()
    expect(page.get_by_role("button", name="Show sidebar")).to_be_visible()
    expect(page.get_by_label("Search notebooks")).to_be_hidden()
    page.get_by_role("button", name="Show sidebar").click()
    expect(page.get_by_label("Search notebooks")).to_be_visible()
    page.reload()
    expect(page.get_by_label("Search notebooks")).to_be_visible()


def test_collapse_and_expand_outputs_hide_the_output_but_keep_the_prompt(live_page, build_agent):
    llm = ScriptedLLM([TextChunk("Port 8443 is the busiest.")])
    page = live_page(agent=build_agent(llm))
    cell = open_new_notebook(page)
    write_prompt(cell, "busiest port?")
    run(cell)
    expect(cell.locator(".reply")).to_contain_text("8443")
    page.get_by_role("button", name="Collapse outputs").click()
    expect(cell.locator(".reply")).to_be_hidden()
    expect(cell.get_by_label("Prompt")).to_be_visible()
    page.get_by_role("button", name="Expand outputs").click()
    expect(cell.locator(".reply")).to_be_visible()


def test_traces_off_hides_only_the_trace_not_the_footer_line_or_context_meter(live_page, build_agent):
    llm = ScriptedLLM([tool_call("run_query", sql=SQL, title="ports")], [TextChunk("Done.")])
    page = live_page(agent=build_agent(llm))
    cell = open_new_notebook(page)
    write_prompt(cell, "ports")
    run(cell)
    expect(cell.locator(".cell-foot")).to_be_visible()
    meter = cell.locator(".cell-foot .ctx")
    expect(meter).to_be_visible()
    expect(cell.locator(".cell-trace .trace-only").first).to_be_visible()
    page.get_by_label("Traces", exact=True).uncheck()
    expect(cell.locator(".cell-trace .trace-only").first).to_be_hidden()
    expect(cell.locator(".cell-foot")).to_be_visible()
    expect(meter).to_be_visible()
    page.get_by_label("Traces", exact=True).check()
    expect(cell.locator(".cell-trace .trace-only").first).to_be_visible()


def test_table_info_off_hides_a_cells_table_name_line(live_page, build_agent):
    page = live_page(agent=build_agent(ScriptedLLM(*table_script())))
    cell = open_new_notebook(page)
    write_prompt(cell, "ports")
    run(cell)
    expect(cell.locator(".table-name").first).to_be_visible()
    page.get_by_label("Table info").uncheck()
    expect(cell.locator(".table-name").first).to_be_hidden()
    expect(cell.locator(".display table").first).to_be_visible()
