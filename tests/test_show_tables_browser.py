"""The Tables dialog, driven in headless system Chrome."""

import pytest
from conftest import ScriptedLLM
from playwright.sync_api import expect
from test_show_tables import two_conversations_with_tables


@pytest.fixture
def tables(build_agent, web_client):
    """Set up through HTTP: two Lookup Tables, a Result Table still named rt_* and one renamed ddos_victims.
    Returns (agent for the live app on the same files, the rt_* name)."""
    client, _, _, rt_name, b_table = two_conversations_with_tables(build_agent, web_client)
    client.post(f"/api/results/{b_table}/settings", data={"name": "ddos_victims", "retention_days": "30"})
    for name in ("watch", "bad_ips"):
        client.post("/api/lookup-tables", data={"name": name, "rows": "ip\n10.0.0.1"})
    return build_agent(ScriptedLLM()), rt_name


def open_tables(page):
    page.get_by_role("button", name="Tables", exact=True).click()
    dialog = page.locator(".show-tables dialog")
    expect(dialog).to_have_attribute("open", "")
    return dialog


def names_in(dialog, heading):
    return dialog.locator("section", has=dialog.page.get_by_role("heading", name=heading, exact=True)).locator("li code")


def test_tables_sits_above_add_lookup_and_lists_the_four_sections(live_page, tables):
    agent, rt_name = tables
    page = live_page(agent=agent)
    show = page.get_by_role("button", name="Tables", exact=True)
    add = page.get_by_role("button", name="Add lookup table")
    assert show.bounding_box()["y"] < add.bounding_box()["y"]  # type: ignore[index]
    assert show.evaluate("b => getComputedStyle(b).borderColor") == add.evaluate("b => getComputedStyle(b).borderColor")

    dialog = open_tables(page)
    headings = dialog.get_by_role("heading", level=4)
    expect(headings).to_have_text(["Telemetry", "Lookup Tables", "Saved Tables", "Result Tables"])
    expect(names_in(dialog, "Telemetry")).to_have_text(["flows"])
    expect(names_in(dialog, "Lookup Tables")).to_have_text(["lookup_bad_ips", "lookup_watch"])
    expect(names_in(dialog, "Saved Tables")).to_have_text(["ddos_victims"])
    expect(names_in(dialog, "Result Tables")).to_have_text([rt_name])

    page.keyboard.press("Escape")
    expect(dialog).not_to_have_attribute("open", "")
    open_tables(page).get_by_role("button", name="Close").click()
    expect(dialog).not_to_have_attribute("open", "")


def test_saved_and_result_tables_show_their_retention_and_the_others_none(live_page, tables):
    agent, rt_name = tables
    dialog = open_tables(live_page(agent=agent))

    def row(name):
        return dialog.locator("li", has=dialog.page.locator("code", has_text=name))

    expect(row("ddos_victims").locator(".retention")).to_have_text("30 days")
    expect(row(rt_name).locator(".retention")).to_have_text("1 day")
    expect(row(rt_name).locator(".retention")).to_have_attribute("title", "Retention: expires 1 day after it was created")
    expect(row("flows").locator(".retention")).to_have_count(0)
    expect(row("lookup_watch").locator(".retention")).to_have_count(0)


@pytest.mark.parametrize("days, shown", [("0.5", "12 hours"), ("1.5", "1.5 days"), ("0.01", "14 minutes")])
def test_retention_under_a_day_is_shown_in_hours_or_minutes(live_page, tables, days, shown):
    agent, rt_name = tables
    agent.update_result_table(rt_name, retention_days=float(days))
    dialog = open_tables(live_page(agent=agent))
    expect(dialog.locator("li", has=dialog.page.locator("code", has_text=rt_name)).locator(".retention")).to_have_text(shown)


def test_checkboxes_hide_result_and_lookup_tables_and_are_remembered_after_reload(live_page, tables):
    agent, rt_name = tables
    page = live_page(agent=agent)
    dialog = open_tables(page)
    hide_rt, hide_lookup = dialog.get_by_label("Hide Result Tables"), dialog.get_by_label("Hide Lookup Tables")
    expect(hide_rt).not_to_be_checked()
    expect(hide_lookup).not_to_be_checked()

    hide_rt.check()
    expect(dialog.get_by_role("heading", name="Result Tables")).to_have_count(0)
    hide_lookup.check()
    expect(dialog.get_by_role("heading", name="Lookup Tables")).to_have_count(0)
    expect(names_in(dialog, "Telemetry")).to_have_text(["flows"])
    expect(names_in(dialog, "Saved Tables")).to_have_text(["ddos_victims"])

    page.reload()
    dialog = open_tables(page)
    expect(dialog.get_by_label("Hide Result Tables")).to_be_checked()
    expect(dialog.get_by_label("Hide Lookup Tables")).to_be_checked()
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


def test_dialog_top_stays_put_when_a_checkbox_changes_the_list(live_page, tables):
    agent, _ = tables
    page = live_page(agent=agent)
    dialog = open_tables(page)
    expect(names_in(dialog, "Result Tables")).to_have_count(1)
    top = dialog.bounding_box()["y"]

    dialog.get_by_label("Hide Lookup Tables").check()
    expect(dialog.get_by_role("heading", name="Lookup Tables")).to_have_count(0)
    assert dialog.bounding_box()["y"] == top
    dialog.get_by_label("Hide Result Tables").check()
    expect(dialog.get_by_role("heading", name="Result Tables")).to_have_count(0)
    assert dialog.bounding_box()["y"] == top


def test_clicking_outside_the_dialog_closes_it_but_clicking_inside_does_not(live_page, tables):
    agent, _ = tables
    page = live_page(agent=agent)
    dialog = open_tables(page)
    box = dialog.bounding_box()

    # Inside: the dialog's own padding (the click target is the <dialog> itself) and its heading.
    page.mouse.click(box["x"] + 4, box["y"] + 4)
    dialog.get_by_role("heading", name="Tables you can query").click()
    expect(dialog).to_have_attribute("open", "")

    page.mouse.click(box["x"] + box["width"] + 20, box["y"] + box["height"] / 2)
    expect(dialog).not_to_have_attribute("open", "")


def test_deleting_from_the_dialog_asks_inline_then_removes_the_row_and_stays_open(live_page, tables):
    agent, rt_name = tables
    page = live_page(agent=agent)
    dialog = open_tables(page)
    expect(dialog.get_by_role("button", name="Delete flows")).to_have_count(0)  # telemetry can't be deleted

    dialog.get_by_role("button", name="Delete ddos_victims").click()
    expect(dialog.get_by_text("Delete ddos_victims?")).to_be_visible()
    dialog.get_by_role("button", name="Cancel").click()
    expect(dialog.get_by_text("Delete ddos_victims?")).to_have_count(0)
    expect(names_in(dialog, "Saved Tables")).to_have_text(["ddos_victims"])

    dialog.get_by_role("button", name="Delete ddos_victims").click()
    dialog.get_by_role("button", name="Delete", exact=True).click()
    expect(dialog.get_by_role("heading", name="Saved Tables")).to_have_count(0)
    expect(dialog).to_have_attribute("open", "")

    dialog.get_by_role("button", name="Delete lookup_watch").click()
    dialog.get_by_role("button", name="Delete", exact=True).click()
    expect(names_in(dialog, "Lookup Tables")).to_have_text(["lookup_bad_ips"])

    page.keyboard.press("Escape")
    dialog = open_tables(page)
    expect(dialog.locator("li code")).to_have_text(["flows", "lookup_bad_ips", rt_name])
