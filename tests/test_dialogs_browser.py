"""The Add lookup table and delete-Conversation dialogs, driven in headless system Chrome."""

from conftest import ScriptedLLM
from playwright.sync_api import expect
from test_conversations import table_script


def add_lookup(page, name, rows):
    page.get_by_role("button", name="Add lookup table").click()
    dialog = page.locator(".lookup dialog")
    dialog.get_by_label("Lookup table name").fill(name)
    dialog.get_by_label("Rows").fill(rows)
    dialog.get_by_role("button", name="Submit").click()
    return dialog


def test_adding_a_lookup_table_closes_the_dialog_and_confirms_on_the_page(live_page):
    page = live_page()
    dialog = add_lookup(page, "watch", "ip,label\n10.0.0.1,bad")
    expect(dialog).not_to_have_attribute("open", "")
    toast = page.get_by_role("status")
    expect(toast).to_have_text("Added lookup_watch: 1 row, columns ip, label.")
    assert not dialog.locator(":scope *", has_text="Added").count()


def test_a_rejected_lookup_table_keeps_the_dialog_open_with_the_error(live_page):
    page = live_page()
    add_lookup(page, "watch", "ip,label\n10.0.0.1,bad")
    expect(page.get_by_role("status")).to_be_visible()
    dialog = add_lookup(page, "watch", "ip,label\n10.0.0.2,x")
    expect(dialog.locator(".error")).to_contain_text("already exists")
    expect(dialog).to_have_attribute("open", "")


def test_delete_dialog_stacks_its_choices_and_hides_the_redundant_rt_option(build_agent, live_page):
    page = live_page(agent=build_agent(ScriptedLLM(*table_script())))
    page.get_by_placeholder("Write a prompt").fill("show me flows")
    page.get_by_role("button", name="Ask").click()
    expect(page.locator(".reply", has_text="Here you go.")).to_be_visible()

    page.get_by_role("button", name="Conversation options").click()
    page.get_by_role("menuitem", name="Delete").click()
    dialog = page.locator("#delete-dialog")
    expect(dialog).to_have_attribute("open", "")
    # The only table is still named rt_*, so "only rt_* tables" would mean the same as "all".
    expect(dialog.locator("#delete-rt-option")).to_be_hidden()

    parts = [dialog.locator("h3"), dialog.get_by_label("Keep all tables"),
             dialog.get_by_label("Delete all its Result Tables"), dialog.locator(".actions")]
    tops = [part.bounding_box()["y"] for part in parts]  # type: ignore[index]
    assert tops == sorted(tops) and len(set(tops)) == len(tops), f"not stacked in rows: {tops}"

    delete = dialog.get_by_role("button", name="Delete")
    red, green, blue = map(int, delete.evaluate("b => getComputedStyle(b).backgroundColor")[4:-1].split(","))
    assert red > 150 and green < 80 and blue < 80, "Delete button should be red"

    dialog.get_by_label("Delete all its Result Tables").check()
    delete.click()
    expect(page.get_by_role("button", name="Conversation options")).to_have_count(0)
