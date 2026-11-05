"""Editing a Lookup Table from the Tables dialog, driven in headless system Chrome."""

from conftest import ScriptedLLM
from playwright.sync_api import expect
from test_lookup_tables import add, stored
from test_show_tables_browser import open_tables, row_in


def edit_dialog(page):
    return page.locator(".lookup dialog")


def test_pencil_shows_only_for_lookup_tables_and_opens_the_rows_to_edit(live_page, build_agent, web_client):
    add(web_client(build_agent(ScriptedLLM())), "hosts", 'ip,desc\n10.0.0.1,"edge, east"')
    agent = build_agent(ScriptedLLM())
    page = live_page(agent=agent)
    tables = open_tables(page)
    expect(row_in(tables, "flows").get_by_role("button", name="Edit flows")).to_be_hidden()
    row_in(tables, "lookup_hosts").get_by_role("button", name="Edit lookup_hosts").click()

    dialog = edit_dialog(page)
    expect(dialog).to_have_attribute("open", "")
    expect(dialog.get_by_role("heading")).to_have_text("Edit lookup_hosts")
    expect(dialog.get_by_label("Lookup table name")).to_have_count(0)  # the name is fixed
    rows = dialog.get_by_label("Rows")
    expect(rows).to_have_value("ip\tdesc\n10.0.0.1\tedge, east\n")
    expect(dialog.get_by_role("radio", name="Tab")).to_be_checked()

    rows.fill("ip\tdesc\n10.0.0.1\tedge, east\n10.0.0.2\tdns\n")
    dialog.get_by_role("radio", name="Comma").check()
    expect(rows).to_have_value('ip,desc\n10.0.0.1,"edge, east"\n10.0.0.2,dns\n')  # the edit is kept
    dialog.get_by_role("radio", name="Pipe").check()
    expect(rows).to_have_value("ip|desc\n10.0.0.1|edge, east\n10.0.0.2|dns\n")

    dialog.get_by_role("button", name="Save").click()
    expect(dialog).not_to_have_attribute("open", "")
    expect(page.locator(".toast")).to_contain_text("Updated lookup_hosts: 2 rows")
    expect(tables).to_have_attribute("open", "")  # back on the Tables dialog
    assert stored(agent, "lookup_hosts") == [["10.0.0.1", "edge, east"], ["10.0.0.2", "dns"]]


def test_bad_edits_show_the_error_keep_the_separator_and_cancel_saves_nothing(live_page, build_agent, web_client):
    add(web_client(build_agent(ScriptedLLM())), "hosts", "ip\n10.0.0.1")
    agent = build_agent(ScriptedLLM())
    page = live_page(agent=agent)
    row_in(open_tables(page), "lookup_hosts").get_by_role("button", name="Edit lookup_hosts").click()
    dialog = edit_dialog(page)
    rows = dialog.get_by_label("Rows")
    expect(rows).to_have_value("ip\n10.0.0.1\n")
    rows.fill("ip\tip\n1\t2")
    dialog.get_by_role("radio", name="Comma").click()
    expect(dialog.locator(".error")).to_contain_text("different")
    expect(dialog.get_by_role("radio", name="Tab")).to_be_checked()
    expect(rows).to_have_value("ip\tip\n1\t2")
    dialog.get_by_role("button", name="Save").click()
    expect(dialog.locator(".error")).to_contain_text("different")
    dialog.get_by_role("button", name="Cancel").click()
    expect(dialog).not_to_have_attribute("open", "")
    assert stored(agent, "lookup_hosts") == [["10.0.0.1"]]

    # Add lookup table afterwards is the plain add dialog again.
    page.keyboard.press("Escape")
    page.get_by_role("button", name="Add lookup table").click()
    expect(dialog.get_by_label("Lookup table name")).to_have_value("")
    expect(dialog.get_by_label("Rows")).to_have_value("")
    expect(dialog.get_by_role("radio")).to_have_count(0)
    expect(dialog.get_by_role("button", name="Submit")).to_be_visible()
