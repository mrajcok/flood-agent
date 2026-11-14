"""The table name header (name + pencil, copy, time left) over a Display, for tables and charts alike."""

from conftest import ScriptedLLM
from playwright.sync_api import expect
from test_chart_tooltip_browser import chart_script
from test_notebook_browser import run_in_new_notebook
from test_rename_browser import ask_for_a_table

CHART = chart_script(
    "SELECT dport, SUM(bytes) AS total_bytes FROM flows GROUP BY dport",
    "bar",
    {"x": {"field": "dport", "type": "nominal"}, "y": {"field": "total_bytes", "type": "quantitative"}},
)


def ask_for_a_chart(build_agent, live_page):
    agent = build_agent(ScriptedLLM(*CHART))
    page = live_page(agent=agent)
    run_in_new_notebook(page, "chart it")
    expect(page.locator(".display.chart canvas")).to_be_visible()
    expect(page.locator(".drawer")).to_have_count(0)  # the layout stops moving once the live trace has folded away
    (table,) = agent.tables.names()
    return agent, page, table


def test_a_chart_shows_its_result_table_name_copy_icon_and_time_left_just_above_it(build_agent, live_page):
    _, page, table = ask_for_a_chart(build_agent, live_page)
    header = page.locator(".display.chart .table-name")
    expect(header.locator(".name")).to_have_text(table)
    expect(header.get_by_role("button", name=f"Copy {table}")).to_be_visible()
    expect(header.locator(".expires")).to_have_text("expires in 1d")
    canvas = page.locator(".display.chart canvas").bounding_box()
    box = header.bounding_box()
    assert canvas and box and 0 <= canvas["y"] - (box["y"] + box["height"]) < 16


def test_renaming_from_a_chart_changes_the_table_and_its_time_left(build_agent, live_page):
    agent, page, table = ask_for_a_chart(build_agent, live_page)
    page.locator(".display.chart .table-name .edit").click()
    page.get_by_label("Table name (SQL)").fill("port_bytes")
    page.get_by_role("button", name="Save").click()  # a rename switches Retention to 30 days
    header = page.locator(".display.chart .table-name")
    expect(header.locator(".name")).to_have_text("port_bytes")
    expect(header.locator(".expires")).to_have_text("expires in 30d")
    assert agent.tables["port_bytes"].retention_days == 30
    page.reload()  # the reopened notebook shows the chart under its new name
    expect(page.locator(".display.chart .table-name .name")).to_have_text("port_bytes")
    expect(page.locator(".display.chart canvas")).to_be_visible()


def test_a_table_display_shows_its_time_left_too(build_agent, live_page):
    agent, page, table = ask_for_a_table(build_agent, live_page)
    expires = page.locator(".display .table-name .expires")
    expect(expires).to_have_text("expires in 1d")
    expect(expires).to_have_attribute("title", "Retention: 1 day")
    page.locator(".table-name .edit").click()
    page.get_by_label("Retention", exact=True).select_option("7")
    page.get_by_role("button", name="Save").click()
    expect(expires).to_have_text("expires in 7d")


def test_a_table_display_follows_a_rename_for_sorting_download_and_further_edits(build_agent, live_page):
    agent, page, table = ask_for_a_table(build_agent, live_page)
    display = page.locator(".display").first
    page.locator(".table-name .edit").click()
    page.get_by_label("Table name (SQL)").fill("kept_flows")
    page.get_by_role("button", name="Save").click()
    expect(display.locator(".table-name .name")).to_have_text("kept_flows")
    expect(display.get_by_role("link", name="Download PSV")).to_have_attribute("href", "/api/results/kept_flows/psv")
    display.locator("th").first.click()  # sorting reloads the rows from the new name
    expect(display.locator("th").first).to_have_attribute("aria-sort", "ascending")
    expect(display.get_by_text("Could not load table.")).to_be_hidden()
    page.locator(".table-name .edit").click()  # a further edit saves under the new name
    page.get_by_label("Retention", exact=True).select_option("7")
    page.get_by_role("button", name="Save").click()
    expect(display.locator(".table-name .expires")).to_have_text("expires in 7d")
    assert agent.tables["kept_flows"].retention_days == 7


def test_a_table_header_has_edit_copy_delete_and_refresh_icons_but_no_view(build_agent, live_page):
    _, page, table = ask_for_a_table(build_agent, live_page)
    header = page.locator(".display .table-name")
    expect(header.get_by_role("button", name=f"Edit {table}")).to_be_visible()
    expect(header.get_by_role("button", name=f"Copy {table}")).to_be_visible()
    expect(header.get_by_role("button", name=f"Delete {table}")).to_be_visible()
    expect(header.get_by_role("button", name=f"View {table}")).to_have_count(0)


def test_a_chart_header_also_has_a_view_icon_in_order(build_agent, live_page):
    _, page, table = ask_for_a_chart(build_agent, live_page)
    labels = page.locator(".display.chart .table-name .icons button").evaluate_all("els => els.map(e => e.getAttribute('aria-label'))")
    assert labels[:4] == [f"Edit {table}", f"Copy {table}", f"View {table}", f"Delete {table}"]


def test_the_delete_icon_asks_then_deletes_the_table(build_agent, live_page):
    agent, page, table = ask_for_a_table(build_agent, live_page)
    header = page.locator(".display .table-name")
    header.get_by_role("button", name=f"Delete {table}").click()
    header.get_by_role("button", name="Cancel").click()  # cancelling keeps it
    assert table in agent.tables.names()
    header.get_by_role("button", name=f"Delete {table}").click()
    header.get_by_role("button", name="Delete", exact=True).click()
    expect(page.get_by_text("this result has been deleted")).to_be_visible()
    assert table not in agent.tables.names()


def test_deleting_from_a_chart_hides_the_chart(build_agent, live_page):
    agent, page, table = ask_for_a_chart(build_agent, live_page)
    page.locator(".display.chart .table-name").get_by_role("button", name=f"Delete {table}").click()
    page.locator(".display.chart .table-name").get_by_role("button", name="Delete", exact=True).click()
    expect(page.get_by_text("this result has been deleted")).to_be_visible()
    expect(page.locator(".display.chart canvas")).to_be_hidden()
    assert table not in agent.tables.names()
