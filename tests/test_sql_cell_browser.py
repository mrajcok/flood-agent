"""SQL Cells in a real browser: run, Stop, Explain and the rows chip."""

import re

from conftest import ScriptedLLM
from playwright.sync_api import expect
from test_sql_cells import SLOW


def add_sql_cell(page):
    page.get_by_role("button", name="New notebook").click()
    expect(page).to_have_url(re.compile(r"/n/\w+$"))
    page.locator(".add-bar").first.get_by_role("button", name="+ SQL").click()
    cell = page.locator(".cellblock.sql")
    expect(cell).to_have_count(1)
    return cell


def test_running_a_sql_cell_shows_its_table_and_status_line_and_run_again_keeps_the_name(live_page, build_agent):
    llm = ScriptedLLM()
    page = live_page(agent=build_agent(llm))
    cell = add_sql_cell(page)
    expect(cell.get_by_role("button", name="Run")).to_be_disabled()
    cell.get_by_label("SQL").fill("SELECT dport FROM flows GROUP BY dport")
    cell.get_by_role("button", name="Run").click()

    expect(cell.locator(".output .display")).to_contain_text("8443")
    expect(cell.locator(".gutter .num")).to_have_text("[1]")
    expect(cell.locator(".sql-status")).to_contain_text(re.compile(r"\d+ rows · [\d.]+ s · ran \d\d:\d\d"))
    expect(cell.locator(".table-name .refresh")).to_be_hidden()  # its Run is its refresh
    name = cell.locator(".table-name .name").inner_text()
    cell.get_by_role("button", name="Run").click()
    expect(cell.locator(".gutter .num")).to_have_text("[2]")
    expect(cell.locator(".table-name .name")).to_have_text(name)
    assert llm.calls == []


def test_the_rows_chip_caps_the_table_and_the_next_sql_cell_starts_with_it(live_page, build_agent):
    page = live_page(agent=build_agent(ScriptedLLM()))
    cell = add_sql_cell(page)
    expect(cell.locator(".chip")).to_have_text("≤ 1,000 rows ▾")
    cell.locator(".chip").click()
    cell.get_by_label("Max rows").fill("1")
    cell.get_by_label("Max rows").press("Tab")
    expect(cell.locator(".chip")).to_have_text("≤ 1 rows ▾")
    cell.get_by_label("SQL").fill("SELECT dport FROM flows")
    cell.get_by_role("button", name="Run").click()
    expect(cell.locator(".sql-status")).to_contain_text("1 row")
    expect(cell.locator(".sql-status")).to_contain_text("1-row cap")

    page.locator(".add-bar").last.get_by_role("button", name="+ SQL").click()
    expect(page.locator(".cellblock.sql").nth(1).locator(".chip")).to_have_text("≤ 1 rows ▾")


def test_explain_shows_the_query_plan_without_running_the_cell(live_page, build_agent):
    page = live_page(agent=build_agent(ScriptedLLM()))
    cell = add_sql_cell(page)
    expect(cell.get_by_role("button", name="Explain")).to_be_disabled()
    cell.get_by_label("SQL").fill("SELECT dport FROM flows WHERE etime > 5")
    cell.get_by_role("button", name="Explain").click()
    dialog = page.locator("dialog.explain")
    expect(dialog.locator(".plan")).to_contain_text("SEARCH flows USING INDEX flows_etime")
    expect(cell.locator(".gutter .num")).to_have_text("[ ]")  # nothing ran


def test_stop_aborts_a_running_sql_cell(live_page, build_agent):
    page = live_page(agent=build_agent(ScriptedLLM(), query_timeout_s=60))
    cell = add_sql_cell(page)
    cell.get_by_label("SQL").fill(SLOW)
    cell.get_by_role("button", name="Run").click()
    expect(cell.locator(".gutter .num")).to_have_text("[*]")
    cell.get_by_role("button", name="Stop").click()
    expect(cell.locator(".cell-stopped")).to_be_visible(timeout=15000)
    expect(cell.locator(".gutter .num")).to_have_text("[1]")
    expect(cell.get_by_role("button", name="Run")).to_be_enabled()
