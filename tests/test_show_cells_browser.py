"""SHOW / DESCRIBE SQL Cells in a real browser: the Command drop-down, the inline sortable rows, reopening."""

import re

from conftest import ScriptedLLM
from playwright.sync_api import expect
from test_sql_cell_browser import add_sql_cell


def test_show_tables_runs_from_the_command_dropdown_with_sortable_rows_that_survive_a_reload(live_page, build_agent):
    llm = ScriptedLLM()
    page = live_page(agent=build_agent(llm))
    cell = add_sql_cell(page)
    cell.get_by_label("Command").select_option("SHOW COLUMNS")
    expect(cell.get_by_label("SQL")).to_be_hidden()
    expect(cell.locator(".sql-tools")).to_be_hidden()
    expect(cell.locator(".chip")).to_be_disabled()
    cell.get_by_role("button", name="Run").click()

    rows = cell.locator(".show-table tbody tr")
    expect(rows.first).to_contain_text("flows")
    expect(rows).to_have_count(11)
    expect(cell.locator(".sql-status")).to_contain_text(re.compile(r"\d+ rows? · [\d.]+ s"))
    expect(cell.locator(".table-name")).to_have_count(0)  # no table was made
    cell.get_by_role("button", name="column_name").click()
    expect(rows.first).to_contain_text("bytes")
    cell.get_by_role("button", name="column_name").click()  # descending
    expect(rows.first).to_contain_text("tcp_flags")

    page.reload()
    cell = page.locator(".cellblock.sql")
    expect(cell.get_by_label("Command")).to_have_value("SHOW COLUMNS")
    expect(cell.locator(".show-table tbody tr").first).to_be_visible()
    assert llm.calls == []


def test_describe_table_needs_a_table_from_the_dropdown(live_page, build_agent):
    page = live_page(agent=build_agent(ScriptedLLM()))
    cell = add_sql_cell(page)
    cell.get_by_label("Command").select_option("DESCRIBE TABLE")
    expect(cell.get_by_role("button", name="Run")).to_be_disabled()
    cell.get_by_role("button", name="Table").click()
    cell.locator(".titem", has_text="flows").click()
    expect(cell.get_by_role("button", name="Run")).to_be_enabled()
    cell.get_by_role("button", name="Run").click()
    expect(cell.locator(".show-table")).to_contain_text("dport")


def test_a_show_typed_into_the_textbox_points_to_the_command_dropdown(live_page, build_agent):
    page = live_page(agent=build_agent(ScriptedLLM()))
    cell = add_sql_cell(page)
    cell.get_by_label("SQL").fill("SHOW TABLES")
    cell.get_by_role("button", name="Run").click()
    expect(cell.locator(".cell-error")).to_contain_text("Command drop-down")
