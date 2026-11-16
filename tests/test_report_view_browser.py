"""Report View in a real browser: the Cells show only what they produced, and nothing can be changed."""

import re

from conftest import ScriptedLLM, add_below
from playwright.sync_api import expect
from test_sql_cell_browser import add_sql_cell

SQL = "SELECT dport FROM flows GROUP BY dport"


def notebook_with_a_report(live_page, build_agent):
    """A new notebook: its never-run AI Cell, a SQL Cell that ran, and a Markdown Cell with a note."""
    page = live_page(agent=build_agent(ScriptedLLM()))
    sql = add_sql_cell(page)
    sql.get_by_label("SQL", exact=True).fill(SQL)
    sql.get_by_role("button", name="Run").click()
    expect(sql.locator(".output .display")).to_contain_text("8443")
    add_below(page, "markdown")
    note = page.locator(".cellblock.markdown")
    note.get_by_label("Markdown").fill("Look at **UDP** first")
    note.get_by_label("Markdown").press("Shift+Enter")
    expect(note.locator(".md-preview")).to_contain_text("Look at")
    return page, sql, note


def test_report_view_keeps_what_the_cells_produced_and_hides_the_machinery(live_page, build_agent):
    page, sql, note = notebook_with_a_report(live_page, build_agent)
    expect(sql.get_by_role("button", name="Run")).to_be_visible()
    expect(sql.locator(".sql-note")).to_be_visible()

    page.get_by_label("Report view").check()

    expect(sql.get_by_label("SQL", exact=True)).to_have_value(SQL)  # the query behind the table stays
    expect(sql.locator(".output .display")).to_contain_text("8443")
    expect(sql.locator(".pager")).to_be_visible()  # a reader can still page
    expect(note.locator(".md-preview")).to_contain_text("Look at UDP first")
    for hidden in (sql.get_by_role("button", name="Run"), sql.locator(".kind"), sql.locator(".chip"), sql.locator(".cell-tools"),
                   sql.locator(".cell-insert"), sql.locator(".sql-note"), sql.locator(".table-name"),
                   sql.get_by_role("link", name="Download PSV"), note.get_by_role("button", name="Edit"), note.locator(".kind")):
        expect(hidden).to_be_hidden()

    page.get_by_label("Report view").uncheck()
    expect(sql.get_by_role("button", name="Run")).to_be_visible()
    expect(sql.locator(".sql-note")).to_be_visible()
    expect(note.get_by_role("button", name="Edit")).to_be_visible()


def test_report_view_leaves_out_cells_with_nothing_to_show(live_page, build_agent):
    page, sql, note = notebook_with_a_report(live_page, build_agent)
    add_below(page, "markdown")  # an empty Markdown Cell, open for editing
    ai = page.locator(".cellblock.ai")
    expect(page.locator(".cellblock:visible")).to_have_count(4)

    page.get_by_label("Report view").check()

    expect(page.locator(".cellblock:visible")).to_have_count(2)  # the SQL Cell and the note
    expect(ai).to_be_hidden()
    page.get_by_label("Report view").uncheck()
    expect(page.locator(".cellblock:visible")).to_have_count(4)


def test_report_view_cannot_edit_a_cell(live_page, build_agent):
    page, sql, note = notebook_with_a_report(live_page, build_agent)
    page.get_by_label("Report view").check()

    note.locator(".md-preview").dblclick()
    expect(note.get_by_label("Markdown")).to_be_hidden()  # double-clicking does not open the editor
    sql.get_by_label("SQL", exact=True).focus()
    page.keyboard.type("x")
    expect(sql.get_by_label("SQL", exact=True)).to_have_value(SQL)


def test_report_view_overrides_the_view_toggles_and_collapsed_outputs_without_changing_them(live_page, build_agent):
    page, sql, note = notebook_with_a_report(live_page, build_agent)
    page.get_by_role("button", name="Collapse outputs").click()
    expect(sql.locator(".output")).to_be_hidden()

    page.get_by_label("Report view").check()
    expect(sql.locator(".output .display")).to_be_visible()  # outputs are shown whatever "Collapse outputs" says
    for name in ("Traces", "Live traces", "Table info"):
        expect(page.get_by_label(name, exact=True)).to_be_disabled()
        expect(page.get_by_label(name, exact=True)).to_be_checked()  # their own values are untouched
    expect(page.get_by_role("button", name="Expand outputs")).to_be_disabled()

    page.get_by_label("Report view").uncheck()
    expect(page.get_by_label("Table info")).to_be_enabled()
    expect(sql.locator(".output")).to_be_hidden()  # collapsed again, as it was


def test_report_view_is_remembered_across_a_reload(live_page, build_agent):
    page, sql, note = notebook_with_a_report(live_page, build_agent)
    page.get_by_label("Report view").check()
    page.reload()
    expect(page.get_by_label("Report view")).to_be_checked()
    expect(page.locator(".cellblock.sql").get_by_role("button", name="Run")).to_be_hidden()
    assert re.search(r"/n/\w+$", page.url)
