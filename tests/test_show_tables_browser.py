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


def row_in(dialog, name):
    return dialog.locator("li", has=dialog.page.locator("code", has_text=name))


def test_saved_and_result_tables_show_when_they_expire_and_the_others_nothing(live_page, tables):
    agent, rt_name = tables
    dialog = open_tables(live_page(agent=agent))
    expect(row_in(dialog, "ddos_victims").locator(".expires")).to_have_text("expires in 30d")
    expect(row_in(dialog, rt_name).locator(".expires")).to_have_text("expires in 1d")
    expect(row_in(dialog, rt_name).locator(".expires")).to_have_attribute("title", "Retention: 1 day")
    expect(row_in(dialog, "flows").locator(".expires")).to_have_count(0)
    expect(row_in(dialog, "lookup_watch").locator(".expires")).to_have_count(0)


# A table made seconds ago with Retention `days` expires in about that long; rounded to the nearest hour,
# and to the nearest day from 7 days on.
@pytest.mark.parametrize("days, shown", [
    (2 + 3 / 24, "expires in 2d 3h"), (0.5, "expires in 12h"), (2 + 10.6 / 24, "expires in 2d 11h"),
    (6 + 23.6 / 24, "expires in 7d"), (7 + 11.6 / 24, "expires in 7d"), (10.6, "expires in 11d"), (0.01, "expires in under 1h")])
def test_time_left_is_rounded_to_hours_and_whole_days_from_a_week(live_page, tables, days, shown):
    agent, rt_name = tables
    agent.update_result_table(rt_name, retention_days=days)
    dialog = open_tables(live_page(agent=agent))
    expect(row_in(dialog, rt_name).locator(".expires")).to_have_text(shown)


@pytest.mark.parametrize("days, shown", [(0.5, "12 hours"), (1.5, "1.5 days"), (0.01, "14 minutes")])
def test_the_tooltip_gives_the_retention_in_hours_or_minutes_under_a_day(live_page, tables, days, shown):
    agent, rt_name = tables
    agent.update_result_table(rt_name, retention_days=days)
    dialog = open_tables(live_page(agent=agent))
    expect(row_in(dialog, rt_name).locator(".expires")).to_have_attribute("title", "Retention: " + shown)


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


def test_the_icons_come_just_before_each_name_and_the_expiry_is_right_aligned(live_page, tables):
    agent, rt_name = tables
    dialog = open_tables(live_page(agent=agent))
    names = dialog.locator("li code")
    expect(names).to_have_count(5)
    lefts = {round(b["x"]) for b in (names.nth(i).bounding_box() for i in range(5)) if b}
    assert len(lefts) == 1  # every name starts at the same x, Telemetry included
    row = row_in(dialog, rt_name)
    copy, name, expires = (row.locator(s).bounding_box() for s in (".copy", "code", ".expires"))
    delete = row.locator(".delete").bounding_box()
    assert copy and delete and name and expires
    assert copy["x"] < delete["x"] < name["x"] and name["x"] - (delete["x"] + delete["width"]) < 12
    list_right = dialog.locator(".table-list").bounding_box()
    assert list_right and list_right["x"] + list_right["width"] - (expires["x"] + expires["width"]) < 12


def test_the_table_buttons_use_the_same_font_as_new_chat(live_page, tables):
    agent, _ = tables
    page = live_page(agent=agent)
    font = "e => { const s = getComputedStyle(e); return [s.fontFamily, s.fontSize, s.fontWeight]; }"
    new_chat = page.get_by_role("link", name="New chat").evaluate(font)
    for name in ("Tables", "Add lookup table"):
        assert page.get_by_role("button", name=name, exact=True).evaluate(font) == new_chat
