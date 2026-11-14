"""The Traces / Live traces / Table info sliders along the top, driven in headless system Chrome."""

from conftest import ScriptedLLM, table_script
from playwright.sync_api import expect
from test_notebook_browser import open_new_notebook, run_in_new_notebook


def test_the_sliders_start_on_and_traces_off_hides_every_trace_and_greys_live_traces(build_agent, live_page):
    page = live_page(agent=build_agent(ScriptedLLM(*table_script())))
    cell = run_in_new_notebook(page, "show me flows")
    expect(cell.locator(".reply", has_text="Here you go.")).to_be_visible()
    traces, live, names = (page.get_by_label(n, exact=True) for n in ("Traces", "Live traces", "Table info"))
    for s in (traces, live, names):
        expect(s).to_be_checked()
    expect(cell.locator(".cell-trace .trace-only").first).to_be_visible()
    traces.uncheck()
    expect(cell.locator(".cell-trace .trace-only").first).to_be_hidden()
    expect(live).to_be_disabled()
    traces.check()
    expect(cell.locator(".cell-trace .trace-only").first).to_be_visible()
    expect(live).to_be_enabled()


def test_a_table_names_choice_saved_before_the_rename_still_hides_the_line(build_agent, live_page):
    page = live_page(agent=build_agent(ScriptedLLM(*table_script())))
    page.evaluate("localStorage.setItem('viewToggles', JSON.stringify({tables: false}))")
    page.reload()
    cell = run_in_new_notebook(page, "show me flows")
    expect(cell.locator(".reply", has_text="Here you go.")).to_be_visible()
    expect(page.get_by_label("Table info")).not_to_be_checked()
    expect(cell.locator(".table-name").first).to_be_hidden()


def test_live_traces_off_hides_the_live_trace_of_a_running_cell(live_page):
    page = live_page()
    open_new_notebook(page)
    page.get_by_label("Live traces").uncheck()
    assert page.evaluate("document.body.classList.contains('hide-live')")
    page.get_by_label("Live traces").check()
    assert not page.evaluate("document.body.classList.contains('hide-live')")


def test_the_choices_survive_a_reload(live_page):
    page = live_page()
    page.get_by_label("Table info").uncheck()
    page.reload()
    expect(page.get_by_label("Table info")).not_to_be_checked()
    assert page.evaluate("document.body.classList.contains('hide-tables')")


def test_the_sliders_do_not_move_when_outputs_are_collapsed(live_page):
    page = live_page()
    open_new_notebook(page)
    switch = page.get_by_label("Traces", exact=True)
    before = switch.bounding_box()
    page.get_by_role("button", name="Collapse outputs").click()
    expect(page.get_by_role("button", name="Expand outputs")).to_be_visible()
    assert switch.bounding_box() == before
