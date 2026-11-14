"""Refresh in a notebook, in a real browser: an AI Cell's Count Display refreshes in place and the cell keeps its Run."""

from conftest import Clock, ScriptedLLM, tool_call
from playwright.sync_api import expect
from test_notebook_browser import open_new_notebook, run, write_prompt

from flood_agent.llm import TextChunk

RECENT_COUNT = "SELECT COUNT(*) FROM flows WHERE etime > now() - 1500"  # 2 at 1700000003000, 1 at ...3600


def test_an_ai_cells_count_display_refreshes_in_place_and_the_cell_keeps_its_run(live_page, build_agent, monkeypatch):
    monkeypatch.setenv("FIXED_NOW_MS", "1700000003000")
    clock = Clock()
    llm = ScriptedLLM([tool_call("run_count_query", sql=RECENT_COUNT, title="Recent")], [TextChunk("Two.")])
    page = live_page(agent=build_agent(llm, clock=clock))
    cell = open_new_notebook(page)
    write_prompt(cell, "how many?")
    run(cell)
    count = cell.locator(".count-display")
    expect(count.locator(".count-value")).to_have_text("2")
    expect(cell.locator(".gutter .num")).to_have_text("[1]")

    monkeypatch.setenv("FIXED_NOW_MS", "1700000003600")
    clock.ms += 3_600_000
    count.get_by_role("button", name="Refresh").click()

    expect(count.locator(".count-value")).to_have_text("1")
    expect(cell.locator(".gutter .num")).to_have_text("[1]")
    page.reload()
    expect(page.locator(".cellblock .count-value")).to_have_text("1")
