"""Notebook management in a real browser: rename, delete, search, hiding the sidebar and the view toggles."""

import re

from conftest import ScriptedLLM, add_below, table_script, tool_call
from playwright.sync_api import expect
from test_notebook_browser import open_new_notebook, run, write_prompt
from test_notebooks import SQL

from flood_agent.llm import TextChunk


def row(page, title):
    return page.locator(".notebook-list li", has_text=title)


def test_rename_a_notebook_from_its_menu(live_page):
    page = live_page()  # the home page opens a first notebook, "Notebook 1"
    row(page, "Notebook 1").get_by_role("button", name="Notebook options").click()
    page.get_by_role("menuitem", name="Rename").click()
    box = page.get_by_label("Notebook name")
    box.fill("Port scans")
    box.press("Enter")
    expect(page).to_have_url(re.compile(r"/n/\w+$"))
    expect(page.locator(".notebook-list li.current a")).to_have_text("Port scans")
    expect(page.locator("h2.nb-title")).to_have_text("Port scans")


def test_delete_a_notebook_through_the_choice_dialog(live_page):
    page = live_page()  # opens "Notebook 1"
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
    page = live_page()  # opens "Notebook 1"
    cell = page.locator(".cellblock").first
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


def test_hiding_the_sidebar_survives_a_reload(live_page):
    page = live_page()
    open_new_notebook(page)
    page.get_by_role("button", name="Hide sidebar").click()
    expect(page.get_by_role("button", name="Show sidebar")).to_be_visible()
    expect(page.get_by_label("Search notebooks")).to_be_hidden()
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


def test_with_outputs_collapsed_one_cell_can_show_and_hide_its_own_output(live_page, build_agent):
    llm = ScriptedLLM([TextChunk("Port 8443 is the busiest.")], [TextChunk("Port 22 is the quietest.")])
    page = live_page(agent=build_agent(llm))
    first = open_new_notebook(page)
    write_prompt(first, "busiest port?")
    run(first)
    expect(first.locator(".reply")).to_contain_text("8443")
    add_below(page, "ai", first)
    second = page.locator(".cellblock").nth(1)
    write_prompt(second, "quietest port?")
    run(second)
    expect(second.locator(".reply")).to_contain_text("22")
    expect(first.get_by_role("button", name="Show output")).to_be_hidden()  # only offered while collapsed

    page.get_by_role("button", name="Collapse outputs").click()
    expect(first.locator(".reply")).to_be_hidden()
    first.get_by_role("button", name="Show output").click()
    expect(first.locator(".reply")).to_be_visible()
    expect(second.locator(".reply")).to_be_hidden()
    first.get_by_role("button", name="Hide output").click()
    expect(first.locator(".reply")).to_be_hidden()

    second.get_by_role("button", name="Show output").click()
    page.get_by_role("button", name="Expand outputs").click()
    expect(first.locator(".reply")).to_be_visible()
    expect(second.locator(".reply")).to_be_visible()
    page.get_by_role("button", name="Collapse outputs").click()  # collapsing again hides the one shown by hand too
    expect(second.locator(".reply")).to_be_hidden()


def test_traces_off_hides_the_trace_line_and_context_meter_but_keeps_status_badges(live_page, build_agent):
    llm = ScriptedLLM([tool_call("run_query", sql=SQL, title="ports")], [TextChunk("Done.")])
    page = live_page(agent=build_agent(llm))
    cell = open_new_notebook(page)
    write_prompt(cell, "ports")
    run(cell)
    meter = cell.locator(".cell-foot .ctx")
    expect(meter).to_be_visible()
    expect(cell.locator(".cell-trace .trace-only").first).to_be_visible()
    page.get_by_label("Traces", exact=True).uncheck()
    expect(cell.locator(".cell-foot")).to_be_hidden()
    expect(meter).to_be_hidden()
    expect(cell.get_by_role("button", name="Open as SQL Cell")).to_be_visible()  # the actions stay
    cell.get_by_label("Prompt").fill("something else")
    expect(cell.locator(".cell-foot .badge.edited")).to_be_visible()  # status badges stay
    cell.get_by_label("Prompt").fill("ports")
    expect(cell.locator(".cell-foot")).to_be_hidden()
    page.get_by_label("Traces", exact=True).check()
    expect(cell.locator(".cell-trace .trace-only").first).to_be_visible()
    expect(meter).to_be_visible()


def test_table_info_off_hides_a_cells_table_name_line(live_page, build_agent):
    page = live_page(agent=build_agent(ScriptedLLM(*table_script())))
    cell = open_new_notebook(page)
    write_prompt(cell, "ports")
    run(cell)
    expect(cell.locator(".table-name").first).to_be_visible()
    page.get_by_label("Table info").uncheck()
    expect(cell.locator(".table-name").first).to_be_hidden()
    expect(cell.locator(".display table").first).to_be_visible()


def test_sql_cell_can_show_and_hide_its_own_output_while_outputs_are_collapsed(live_page, build_agent):
    page = live_page(agent=build_agent(ScriptedLLM()))
    open_new_notebook(page)
    add_below(page, "sql")
    cell = page.locator(".cellblock.sql")
    cell.get_by_label("SQL").fill("SELECT dport FROM flows GROUP BY dport")
    cell.get_by_role("button", name="Run").click()
    expect(cell.locator(".output .display")).to_contain_text("8443")
    page.get_by_role("button", name="Collapse outputs").click()
    expect(cell.locator(".output .display")).to_be_hidden()
    cell.get_by_role("button", name="Show output").click()
    expect(cell.locator(".output .display")).to_be_visible()
    cell.get_by_role("button", name="Hide output").click()
    expect(cell.locator(".output .display")).to_be_hidden()


def test_markdown_cell_output_toggle_is_offered_only_when_not_editing_and_survives_rendering(live_page):
    page = live_page()
    open_new_notebook(page)
    add_below(page, "markdown")
    cell = page.locator(".cellblock.markdown")
    cell.get_by_label("Markdown").fill("**focus** on UDP")
    cell.get_by_role("button", name="Done").click()
    expect(cell.locator(".md-preview strong")).to_have_text("focus")
    page.get_by_role("button", name="Collapse outputs").click()
    expect(cell.locator(".md-preview")).to_be_hidden()
    cell.get_by_role("button", name="Show output").click()
    expect(cell.locator(".md-preview")).to_be_visible()
    cell.get_by_role("button", name="Edit").click()
    expect(cell.get_by_role("button", name="Hide output")).to_be_hidden()  # nothing to hide while editing
    cell.get_by_role("button", name="Done").click()
    expect(cell.locator(".md-preview")).to_be_visible()  # still open by hand after the re-render
    cell.get_by_role("button", name="Hide output").click()
    expect(cell.locator(".md-preview")).to_be_hidden()
