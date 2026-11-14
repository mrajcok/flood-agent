"""A chart's table-name line has a View button that shows the table in a dialog, as the Tables dialog does."""

from conftest import ScriptedLLM
from playwright.sync_api import expect
from test_chart_tooltip_browser import chart_script
from test_notebook_browser import run_in_new_notebook


def chart_page(live_page, build_agent):
    script = chart_script(
        "SELECT dport, bytes FROM flows WHERE bytes = 987654",
        "bar",
        {"x": {"field": "dport", "type": "nominal"}, "y": {"field": "bytes", "type": "quantitative"}},
    )
    page = live_page(agent=build_agent(ScriptedLLM(*script)))
    run_in_new_notebook(page, "chart it")
    expect(page.locator(".display.chart canvas, .display.chart svg.marks").first).to_be_visible()
    expect(page.locator(".drawer")).to_have_count(0)
    return page


def test_view_on_a_chart_opens_its_table_in_a_dialog(live_page, build_agent):
    page = chart_page(live_page, build_agent)
    page.locator(".display.chart").get_by_role("button", name="View").click()
    viewer = page.locator("dialog.table-viewer")
    expect(viewer).to_be_visible()
    expect(viewer.locator("table")).to_contain_text("8443")
    expect(viewer.locator("table")).to_contain_text("987.65 KB")
    viewer.get_by_role("button", name="Close").click()
    expect(viewer).to_be_hidden()


def test_the_table_shown_in_the_dialog_has_no_view_button_of_its_own(live_page, build_agent):
    page = chart_page(live_page, build_agent)
    page.locator(".display.chart").get_by_role("button", name="View").click()
    expect(page.locator("dialog.table-viewer table")).to_be_visible()
    expect(page.locator("dialog.table-viewer").get_by_role("button", name="View")).to_have_count(0)
