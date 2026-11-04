"""The Show tables dialog, driven in headless system Chrome."""

import pytest
from conftest import ScriptedLLM
from playwright.sync_api import expect
from test_conversations import ask, table_script
from test_show_tables import table_of


@pytest.fixture
def tables(build_agent, web_client):
    """Set up through HTTP: two Lookup Tables, a Result Table still named rt_* and one renamed ddos_victims.
    Returns (agent for the live app on the same files, the rt_* name)."""
    client = web_client(build_agent(ScriptedLLM(*table_script(), *table_script())))
    a, _ = ask(client, "top ports")
    b, _ = ask(client, "top talkers")
    rt_name = table_of(client, a)
    client.post(f"/api/results/{table_of(client, b)}/settings", data={"name": "ddos_victims"})
    for name in ("watch", "bad_ips"):
        client.post("/api/lookup-tables", data={"name": name, "rows": "ip\n10.0.0.1"})
    return build_agent(ScriptedLLM()), rt_name


def open_tables(page):
    page.get_by_role("button", name="Show tables").click()
    dialog = page.locator(".show-tables dialog")
    expect(dialog).to_have_attribute("open", "")
    return dialog


def names_in(dialog, heading):
    return dialog.locator("section", has=dialog.page.get_by_role("heading", name=heading, exact=True)).locator("li code")


def test_show_tables_sits_above_add_lookup_and_lists_the_three_sections(live_page, tables):
    agent, rt_name = tables
    page = live_page(agent=agent)
    show = page.get_by_role("button", name="Show tables")
    add = page.get_by_role("button", name="Add lookup table")
    assert show.bounding_box()["y"] < add.bounding_box()["y"]  # type: ignore[index]
    assert show.evaluate("b => getComputedStyle(b).borderColor") == add.evaluate("b => getComputedStyle(b).borderColor")

    dialog = open_tables(page)
    headings = dialog.get_by_role("heading", level=4)
    expect(headings).to_have_text(["Telemetry", "Lookup Tables", "Result Tables"])
    expect(names_in(dialog, "Telemetry")).to_have_text(["flows"])
    expect(names_in(dialog, "Lookup Tables")).to_have_text(["lookup_bad_ips", "lookup_watch"])
    expect(names_in(dialog, "Result Tables")).to_have_text(["ddos_victims", rt_name])

    page.keyboard.press("Escape")
    expect(dialog).not_to_have_attribute("open", "")
    open_tables(page).get_by_role("button", name="Close").click()
    expect(dialog).not_to_have_attribute("open", "")


def test_checkboxes_hide_by_prefix_and_are_remembered_after_reload(live_page, tables):
    agent, rt_name = tables
    page = live_page(agent=agent)
    dialog = open_tables(page)
    hide_rt, hide_lookup = dialog.get_by_label("Hide rt_ tables"), dialog.get_by_label("Hide lookup_ tables")
    expect(hide_rt).not_to_be_checked()
    expect(hide_lookup).not_to_be_checked()

    hide_rt.check()
    expect(names_in(dialog, "Result Tables")).to_have_text(["ddos_victims"])
    hide_lookup.check()
    expect(dialog.get_by_role("heading", name="Lookup Tables")).to_have_count(0)
    expect(names_in(dialog, "Telemetry")).to_have_text(["flows"])
    expect(names_in(dialog, "Result Tables")).to_have_text(["ddos_victims"])

    page.reload()
    dialog = open_tables(page)
    expect(dialog.get_by_label("Hide rt_ tables")).to_be_checked()
    expect(dialog.get_by_label("Hide lookup_ tables")).to_be_checked()
    expect(dialog.locator("li code")).to_have_text(["flows", "ddos_victims"])
    assert rt_name not in dialog.inner_text()


def test_copying_inserts_the_name_at_the_cursor_closes_the_dialog_and_focuses_the_input(live_page, tables):
    agent, _ = tables
    page = live_page(agent=agent)  # headless Chrome grants no clipboard permission: writeText rejects
    chat = page.get_by_placeholder("Write a prompt")
    chat.fill("count me rows")
    chat.evaluate("i => i.setSelectionRange(6, 6)")  # "count |me rows": touches "me" on its right

    dialog = open_tables(page)
    dialog.get_by_role("button", name="Copy ddos_victims").click()
    expect(dialog).not_to_have_attribute("open", "")
    expect(chat).to_have_value("count ddos_victims me rows")
    expect(chat).to_be_focused()
    assert chat.evaluate("i => [i.selectionStart, i.selectionEnd]") == [18, 18]

    chat.evaluate("i => i.setSelectionRange(i.value.length, i.value.length)")  # touches "rows" on its left
    open_tables(page).get_by_role("button", name="Copy flows").click()
    expect(chat).to_have_value("count ddos_victims me rows flows")


def test_copying_still_inserts_when_there_is_no_clipboard_api(live_page, tables):
    agent, _ = tables
    page = live_page(agent=agent)
    page.evaluate("() => Object.defineProperty(navigator, 'clipboard', { value: undefined })")
    errors = []
    page.on("pageerror", lambda e: errors.append(e))
    open_tables(page).get_by_role("button", name="Copy flows").click()
    expect(page.get_by_placeholder("Write a prompt")).to_have_value("flows")
    assert errors == []
