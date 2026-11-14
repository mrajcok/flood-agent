"""SQL Cells in a real browser: run, Stop, Explain and the rows chip."""

import re

from conftest import ScriptedLLM
from playwright.sync_api import expect
from test_sql_cells import SLOW


def add_sql_cell(page):
    page.get_by_role("button", name="New notebook").click()
    expect(page).to_have_url(re.compile(r"/n/\w+$"))
    page.locator(".nb-head").get_by_role("button", name="+ SQL").click()
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

    page.locator(".nb-head").get_by_role("button", name="+ SQL").click()
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


def pick_table(cell, name):
    cell.get_by_role("button", name="Table ▾").click()
    cell.get_by_role("button", name=name, exact=True).click()


def test_the_table_dropdown_is_grouped_and_searchable_and_starts_a_query_in_an_empty_cell(live_page, build_agent):
    page = live_page(agent=build_agent(ScriptedLLM()))
    cell = add_sql_cell(page)
    cell.get_by_label("SQL").fill("SELECT dport AS x FROM flows")
    cell.get_by_role("button", name="Run").click()
    expect(cell.locator(".output .display")).to_be_visible()
    expect(cell.locator(".table-name .name")).to_have_text(re.compile(r"rt_\d+"))
    result = cell.locator(".table-name .name").inner_text()

    page.locator(".nb-head").get_by_role("button", name="+ SQL").click()
    second = page.locator(".cellblock.sql").nth(1)
    second.get_by_role("button", name="Table ▾").click()
    expect(second.locator(".tlabel")).to_have_text(["Telemetry", "Result"])
    second.get_by_label("Search tables").fill("RT_")
    expect(second.locator(".titem")).to_have_text([result])
    second.get_by_label("Search tables").fill("flo")
    expect(second.locator(".titem")).to_have_text(["flows"])
    second.get_by_role("button", name="flows", exact=True).click()

    box = second.get_by_label("SQL", exact=True)
    expect(box).to_have_value("SELECT \nFROM flows")
    assert box.evaluate("e => e.selectionStart") == 7
    expect(second.locator(".tbl-cols .col")).to_have_count(11)
    expect(second.locator(".tbl-cols .col").nth(3)).to_have_attribute("title", "INTEGER")


def test_clicking_columns_builds_a_list_at_the_cursor_and_the_table_name_goes_in_too(live_page, build_agent):
    page = live_page(agent=build_agent(ScriptedLLM()))
    cell = add_sql_cell(page)
    pick_table(cell, "flows")
    box = cell.get_by_label("SQL")
    cols = cell.locator(".tbl-cols")
    cols.get_by_role("button", name="sip", exact=True).click()
    expect(box).to_have_value("SELECT sip\nFROM flows")
    cols.get_by_role("button", name="dport", exact=True).click()
    cols.get_by_role("button", name="bytes", exact=True).click()
    expect(box).to_have_value("SELECT sip, dport, bytes\nFROM flows")

    box.fill("SELECT * FROM x WHERE ")
    cols.get_by_role("button", name="etime", exact=True).click()
    expect(box).to_have_value("SELECT * FROM x WHERE etime")
    box.fill("SELECT count(*) FROM ")
    cols.get_by_role("button", name="flows", exact=True).click()
    expect(box).to_have_value("SELECT count(*) FROM flows")
    # a click in the middle inserts at the cursor
    box.fill("SELECT sip FROM flows")
    box.evaluate("e => e.setSelectionRange(10, 10)")
    with page.expect_response(lambda r: r.url.endswith("/draft")):
        cols.get_by_role("button", name="dport", exact=True).click()
    expect(box).to_have_value("SELECT sip, dport FROM flows")
    # the insertion is kept as the Draft
    page.reload()
    expect(page.locator(".cellblock.sql").get_by_label("SQL")).to_have_value("SELECT sip, dport FROM flows")


def test_a_keyword_column_is_quoted_on_insert_and_unquoted_one_is_rejected_with_a_hint(live_page, build_agent):
    page = live_page(agent=build_agent(ScriptedLLM()))
    cell = add_sql_cell(page)
    cell.get_by_label("SQL").fill('SELECT dport AS "analyze" FROM flows')
    cell.get_by_role("button", name="Run").click()
    expect(cell.locator(".output .display")).to_be_visible()
    expect(cell.locator(".table-name .name")).to_have_text(re.compile(r"rt_\d+"))
    result = cell.locator(".table-name .name").inner_text()

    page.locator(".nb-head").get_by_role("button", name="+ SQL").click()
    second = page.locator(".cellblock.sql").nth(1)
    pick_table(second, result)
    second.locator(".tbl-cols").get_by_role("button", name="analyze", exact=True).click()
    expect(second.get_by_label("SQL", exact=True)).to_have_value(f'SELECT "analyze"\nFROM {result}')
    second.get_by_role("button", name="Run").click()
    expect(second.locator(".sql-status")).to_contain_text("3 rows")

    second.get_by_label("SQL", exact=True).fill(f"SELECT analyze FROM {result}")
    second.get_by_role("button", name="Run").click()
    expect(second.locator(".sql-error")).to_contain_text("put it in double quotes")


def test_editing_a_ran_sql_cell_shows_the_edited_not_run_badge_until_it_is_back_to_what_ran(live_page, build_agent):
    page = live_page(agent=build_agent(ScriptedLLM()))
    cell = add_sql_cell(page)
    badge = cell.get_by_text("edited, not run")
    cell.get_by_label("SQL", exact=True).fill("SELECT dport FROM flows")
    expect(badge).to_have_count(0)  # never run
    cell.get_by_role("button", name="Run").click()
    expect(cell.locator(".sql-status")).to_be_visible()
    expect(badge).to_have_count(0)  # no element when not edited

    cell.get_by_label("SQL", exact=True).fill("SELECT sip FROM flows")
    expect(badge).to_be_visible()
    expect(cell.locator(".output .display")).to_be_visible()  # the old output stays
    cell.get_by_label("SQL", exact=True).fill("SELECT dport FROM flows")
    expect(badge).to_have_count(0)
