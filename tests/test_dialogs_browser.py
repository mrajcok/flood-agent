"""The Create lookup table and delete notebook / cell dialogs, driven in headless system Chrome."""

from conftest import ScriptedLLM, table_script
from playwright.sync_api import expect
from test_notebook_browser import run_in_new_notebook

from flood_agent.llm import TextChunk


def add_lookup(page, name, rows):
    page.get_by_role("button", name="Create lookup table").click()
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


def ask_for_a_table(page):
    cell = run_in_new_notebook(page, "show me flows")
    expect(cell.locator(".reply", has_text="Here you go.")).to_be_visible()
    return cell


def open_notebook_delete(page):
    page.locator(".notebook-list li.current").get_by_role("button", name="Notebook options").click()
    page.get_by_role("menuitem", name="Delete").click()
    dialog = page.locator("#delete-dialog")
    expect(dialog).to_have_attribute("open", "")
    return dialog


def no_native_dialogs(page):
    seen = []
    page.on("dialog", lambda d: (seen.append(d.message), d.dismiss()))
    return seen


def test_delete_dialog_offers_to_keep_or_delete_its_result_tables(build_agent, live_page):
    page = live_page(agent=build_agent(ScriptedLLM(*table_script())))
    ask_for_a_table(page)
    dialog = open_notebook_delete(page)
    expect(dialog.locator("h3")).to_have_text("Delete this notebook?")
    keep, delete_rt = dialog.get_by_label("Keep its 1 Result Table"), dialog.get_by_label("Delete its 1 Result Table now")
    expect(keep).to_be_checked()
    expect(dialog).to_contain_text("rt_")
    expect(dialog.get_by_text("Saved Table")).to_be_hidden()

    parts = [dialog.locator("h3"), keep, delete_rt, dialog.locator(".actions")]
    tops = [part.bounding_box()["y"] for part in parts]  # type: ignore[index]
    assert tops == sorted(tops) and len(set(tops)) == len(tops), f"not stacked in rows: {tops}"

    delete = dialog.get_by_role("button", name="Delete")
    red, green, blue = map(int, delete.evaluate("b => getComputedStyle(b).backgroundColor")[4:-1].split(","))
    assert red > 150 and green < 80 and blue < 80, "Delete button should be red"

    delete_rt.check()
    delete.click()
    expect(page.locator(".notebook-list li")).to_have_count(0)


def test_deleting_a_notebook_without_tables_still_uses_the_custom_dialog(build_agent, live_page):
    page = live_page(agent=build_agent(ScriptedLLM([TextChunk("hello there")])))
    native = no_native_dialogs(page)
    cell = run_in_new_notebook(page, "hi")
    expect(cell.locator(".reply", has_text="hello there")).to_be_visible()

    dialog = open_notebook_delete(page)
    expect(dialog.locator("h3")).to_have_text("Delete this notebook?")
    expect(dialog.get_by_role("radio")).to_have_count(0)
    expect(dialog.get_by_text("Saved Table")).to_be_hidden()
    dialog.get_by_role("button", name="Delete").click()
    expect(page.locator(".notebook-list li")).to_have_count(0)
    assert native == []


def test_deleting_a_cell_with_a_saved_table_says_the_saved_table_is_kept(build_agent, live_page):
    agent = build_agent(ScriptedLLM(*table_script()))
    page = live_page(agent=agent)
    native = no_native_dialogs(page)
    cell = ask_for_a_table(page)
    (table,) = agent.tables.names()
    agent.tables.update(table, "keeper")
    page.reload()  # the Saved Table's name shows on the cell's Display

    page.get_by_role("button", name="Delete cell").first.click()
    dialog = page.locator("#delete-dialog")
    expect(dialog).to_have_attribute("open", "")
    expect(dialog.get_by_role("radio")).to_have_count(0)
    expect(dialog).to_contain_text("Its 1 Saved Table will be kept. Use the Tables button to delete Saved (renamed) Tables.")
    dialog.get_by_role("button", name="Cancel").click()
    expect(dialog).not_to_have_attribute("open", "")
    expect(cell).to_be_visible()
    assert native == []


def test_deleting_a_turn_keeps_collapsed_turns_collapsed(build_agent, live_page):
    page = live_page(agent=build_agent(ScriptedLLM([TextChunk("One.")], [TextChunk("Two.")])))
    for text, reply in (("first", "One."), ("second", "Two.")):
        page.get_by_placeholder("Write a prompt").fill(text)
        page.get_by_role("button", name="Send message (Enter)").click()
        expect(page.locator(".reply", has_text=reply)).to_be_visible()
    page.locator("aside ul li a").first.click()
    page.get_by_role("button", name="Collapse turns").click()
    expect(page.locator("details.turn[open]")).to_have_count(0)

    page.get_by_role("button", name="Delete this turn").first.click(force=True)
    page.locator("#delete-dialog").get_by_role("button", name="Delete").click()
    expect(page.locator("details.turn")).to_have_count(1)
    expect(page.locator("details.turn[open]")).to_have_count(0)
    expect(page.get_by_role("button", name="Expand turns")).to_be_visible()


def test_loading_a_file_fills_the_rows_and_suggests_a_name(live_page, tmp_path):
    page = live_page()
    f = tmp_path / "bad-ips.csv"
    f.write_text("ip,label\n10.0.0.1,bad\n10.0.0.2,worse\n")
    page.get_by_role("button", name="Create lookup table").click()
    dialog = page.locator(".lookup dialog")
    dialog.get_by_label("File to load").set_input_files(str(f))
    expect(dialog.get_by_label("Rows")).to_have_value("ip,label\n10.0.0.1,bad\n10.0.0.2,worse\n")
    expect(dialog.get_by_label("Lookup table name")).to_have_value("bad_ips")
    dialog.get_by_role("button", name="Submit").click()
    expect(page.get_by_role("status")).to_have_text("Added lookup_bad_ips: 2 rows, columns ip, label.")


def test_loading_a_file_keeps_a_name_already_typed(live_page, tmp_path):
    page = live_page()
    f = tmp_path / "x.csv"
    f.write_text("a,b\n1,2\n")
    page.get_by_role("button", name="Create lookup table").click()
    dialog = page.locator(".lookup dialog")
    dialog.get_by_label("Lookup table name").fill("mine")
    dialog.get_by_label("File to load").set_input_files(str(f))
    expect(dialog.get_by_label("Rows")).to_have_value("a,b\n1,2\n")
    expect(dialog.get_by_label("Lookup table name")).to_have_value("mine")
