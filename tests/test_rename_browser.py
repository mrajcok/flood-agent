"""Renaming a Result Table in its table Display makes it a Saved Table, kept 30 days by default."""

from conftest import ScriptedLLM
from playwright.sync_api import expect
from test_conversations import table_script


def ask_for_a_table(build_agent, live_page, days=None):
    agent = build_agent(ScriptedLLM(*table_script()))
    page = live_page(agent=agent)
    page.get_by_placeholder("Write a prompt").fill("show me flows")
    page.get_by_role("button", name="Send message (Enter)").click()
    expect(page.locator(".reply", has_text="Here you go.")).to_be_visible()
    (table,) = agent.tables.names()
    if days is not None:
        agent.tables.update(table, retention_days=days)
        page.reload()
        page.locator("aside ul li a").first.click()
    return agent, page, table


def edit(page, table):
    page.locator(".table-name .name").click()
    return page.get_by_label("Table name (SQL)"), page.get_by_label("Retention", exact=True)


def retention_of(agent, name):
    return agent.tables[name].retention_days


def test_renaming_a_result_table_switches_its_retention_to_30_days(build_agent, live_page):
    agent, page, table = ask_for_a_table(build_agent, live_page)
    name, retention = edit(page, table)
    expect(retention).to_have_value("1")
    expect(retention.locator("option:checked")).to_have_text("1 day (rt_ default)")

    name.fill("keeper")
    expect(retention).to_have_value("30")
    name.fill(table)  # back to its own name: nothing to save beyond the original
    expect(retention).to_have_value("1")
    name.fill("keeper")
    page.get_by_role("button", name="Save").click()
    expect(page.locator(".table-name .name")).to_have_text("keeper")
    assert retention_of(agent, "keeper") == 30

    # Renaming a Saved Table again leaves its Retention alone.
    name, retention = edit(page, "keeper")
    retention.select_option("7")
    page.get_by_role("button", name="Save").click()
    name, retention = edit(page, "keeper")
    name.fill("keeper2")
    expect(retention).to_have_value("7")


def test_a_retention_the_user_picks_in_the_same_edit_wins(build_agent, live_page):
    agent, page, table = ask_for_a_table(build_agent, live_page)
    name, retention = edit(page, table)
    retention.select_option("7")
    name.fill("keeper")
    expect(retention).to_have_value("7")
    page.get_by_role("button", name="Save").click()
    expect(page.locator(".table-name .name")).to_have_text("keeper")
    assert retention_of(agent, "keeper") == 7


def test_a_result_table_kept_longer_than_30_days_is_not_cut_back(build_agent, live_page):
    _, page, table = ask_for_a_table(build_agent, live_page, days=365)
    name, retention = edit(page, table)
    expect(retention).to_have_value("365")
    name.fill("keeper")
    expect(retention).to_have_value("365")


def test_the_copy_icon_next_to_the_table_name_inserts_it_into_the_chat_like_the_tables_dialog(build_agent, live_page):
    _, page, table = ask_for_a_table(build_agent, live_page)
    expect(page.get_by_role("button", name="Copy", exact=True)).to_have_count(0)  # the old text button is gone
    chat = page.get_by_placeholder("Write a prompt")
    chat.fill("count rows")
    chat.evaluate("i => i.setSelectionRange(6, 6)")  # "count |rows"
    copy = page.locator(".table-name").get_by_role("button", name="Copy " + table)
    expect(copy).to_have_text("⧉")  # the Tables dialog's copy icon
    copy.click()
    expect(chat).to_have_value(f"count {table} rows")
    expect(chat).to_be_focused()

    page.locator(".table-name .name").click()  # rename it: the icon copies the new name
    page.get_by_label("Table name (SQL)").fill("victims")
    page.get_by_role("button", name="Save").click()
    chat.fill("")
    page.locator(".table-name").get_by_role("button", name="Copy victims").click()
    expect(chat).to_have_value("victims")


def test_the_table_name_has_the_same_pointer_cursor_as_the_copy_icon(build_agent, live_page):
    _, page, _ = ask_for_a_table(build_agent, live_page)
    cursor = "e => getComputedStyle(e).cursor"
    assert page.locator(".table-name .name").evaluate(cursor) == page.locator(".table-name .copy").evaluate(cursor) == "pointer"
