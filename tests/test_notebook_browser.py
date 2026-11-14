"""The Notebook page in a real browser: creating a notebook, running, stopping and the settings chip."""

import re

from conftest import ScriptedLLM, last_tool_result, tool_call
from playwright.sync_api import expect
from test_notebooks import SQL
from test_stop_button_browser import GatedLLM
from test_stop_turn import HangingLLM

from flood_agent.llm import TextChunk


def open_new_notebook(page):
    page.get_by_role("button", name="New notebook").click()
    expect(page).to_have_url(re.compile(r"/n/\w+$"))
    return page.locator(".cellblock").first


def write_prompt(cell, text):
    cell.get_by_label("Prompt").fill(text)


def run(cell):
    cell.get_by_role("button", name="Run").click()


def test_the_sidebar_plus_makes_notebook_n_with_one_empty_ai_cell_and_opens_it(live_page):
    page = live_page()
    expect(page.locator(".notebook-list li")).to_have_count(0)
    cell = open_new_notebook(page)

    expect(page.locator(".notebook-list li.current a")).to_have_text("Notebook 1")
    expect(page.locator(".cellblock")).to_have_count(1)
    expect(cell).to_have_attribute("data-kind", "ai")
    expect(cell.get_by_label("Prompt")).to_have_value("")
    expect(cell.locator(".gutter .num")).to_have_text("[ ]")
    expect(cell.get_by_role("button", name="Run")).to_be_disabled()  # nothing to run yet
    write_prompt(cell, "hello")
    expect(cell.get_by_role("button", name="Run")).to_be_enabled()

    page.get_by_role("button", name="New notebook").click()
    expect(page.locator(".notebook-list li.current a")).to_have_text("Notebook 2")
    expect(page.locator(".notebook-list li")).to_have_count(2)


def test_running_a_cell_streams_the_answer_with_live_steps_then_the_footer(live_page, build_agent):
    llm = GatedLLM([tool_call("run_query", sql=SQL, title="ports")], [TextChunk("Port 8443 is the busiest.")])
    page = live_page(agent=build_agent(llm))
    cell = open_new_notebook(page)
    write_prompt(cell, "which port is busiest?")
    run(cell)

    # Mid-run: [*], a Stop button, the live steps in a drawer under the answer, no footer yet.
    expect(cell.locator(".gutter .num")).to_have_text("[*]")
    expect(cell.get_by_role("button", name="Stop")).to_be_visible()
    expect(cell.get_by_role("button", name="Run")).to_have_count(0)
    expect(cell.locator(".drawer .live-step")).to_have_count(2)
    expect(cell.locator(".cell-foot")).to_have_count(0)
    expect(cell.get_by_label("Prompt")).to_have_attribute("readonly", "")
    llm.gate.set()

    expect(cell.locator(".gutter .num")).to_have_text("[1]")
    expect(cell.locator(".output")).to_contain_text("Port 8443 is the busiest.")
    expect(cell.locator(".drawer")).to_have_count(0)  # the drawer goes with the live view
    foot = cell.locator(".cell-foot")
    expect(foot).to_contain_text(re.compile(r"Trace · ran \d\d:\d\d.* with GLM 5\.3 · max · ≤ 1,000 rows · 2 LLM calls · \$0\.002"))
    expect(foot.locator(".ctx")).to_contain_text("100 tokens of 1.05M")
    expect(foot.locator(".badge")).to_have_count(0)
    expect(cell.get_by_role("button", name="Run")).to_be_enabled()
    expect(cell.locator(".display")).to_have_count(0)  # no table was shown

    foot.locator("details.cell-trace > summary").click()  # the Trace opens in place
    expect(foot).to_contain_text("System prompt")


def test_a_run_that_shows_a_table_keeps_it_in_the_cell_after_a_reload(live_page, build_agent):
    llm = ScriptedLLM(
        [tool_call("run_query", sql=SQL, title="ports")],
        lambda msgs: [tool_call("show_table", result_table=last_tool_result(msgs)["result_table"])],
        [TextChunk("Here you go.")],
    )
    page = live_page(agent=build_agent(llm))
    cell = open_new_notebook(page)
    write_prompt(cell, "show ports")
    run(cell)
    expect(cell.locator(".output .display")).to_have_count(1)
    expect(cell.locator(".output .display")).to_contain_text("8443")
    page.reload()
    cell = page.locator(".cellblock").first
    expect(cell.locator(".output .display")).to_contain_text("8443")
    expect(cell.locator(".output")).to_contain_text("Here you go.")
    expect(cell.locator(".gutter .num")).to_have_text("[1]")
    expect(cell.get_by_label("Prompt")).to_have_value("show ports")


def test_stop_in_the_gutter_stops_the_cell_and_the_footer_says_so(live_page, build_agent):
    page = live_page(agent=build_agent(HangingLLM()))
    cell = open_new_notebook(page)
    write_prompt(cell, "slow one")
    run(cell)
    expect(cell.locator(".output")).to_contain_text("Partial answer")
    cell.get_by_role("button", name="Stop").click()

    expect(cell.locator(".cell-foot .badge.stopped")).to_have_text("stopped")
    expect(cell.locator(".output")).to_contain_text("Partial answer")
    expect(cell.locator(".output .cell-stopped")).to_have_text("stopped by the user")
    expect(cell.locator(".gutter .num")).to_have_text("[1]")
    expect(cell.get_by_role("button", name="Run")).to_be_enabled()
    expect(cell.get_by_role("button", name="Stop")).to_have_count(0)
    page.reload()  # it is the stored Run that shows it
    expect(page.locator(".cellblock .badge.stopped")).to_have_count(1)
    expect(page.locator(".cellblock .cell-stopped")).to_have_count(1)


def test_only_one_cell_runs_at_a_time_in_a_notebook(live_page, build_agent):
    page = live_page(agent=build_agent(HangingLLM()))
    first = open_new_notebook(page)
    first.locator("+ .add-bar").get_by_role("button", name="+ AI").click()
    expect(page.locator(".cellblock")).to_have_count(2)
    second = page.locator(".cellblock").nth(1)
    write_prompt(first, "one")
    write_prompt(second, "two")
    run(first)
    expect(first.get_by_role("button", name="Stop")).to_be_visible()
    expect(second.get_by_role("button", name="Run")).to_be_disabled()
    first.get_by_role("button", name="Stop").click()
    expect(second.get_by_role("button", name="Run")).to_be_enabled()


def test_the_settings_chip_opens_a_popover_for_model_effort_and_max_rows(live_page, build_agent):
    llm = ScriptedLLM([TextChunk("done")])
    page = live_page(agent=build_agent(llm))
    cell = open_new_notebook(page)
    chip = cell.locator(".chip")
    expect(chip).to_have_text(re.compile(r"^.+ · (high|low|max|medium|xhigh|no effort) · ≤ 1,000 rows ▾$"))
    pop = cell.get_by_role("dialog", name="AI Cell settings")
    expect(pop).to_be_hidden()

    chip.click()
    expect(pop).to_be_visible()
    pop.get_by_label("Model").select_option("anthropic/claude-sonnet-5.5")
    pop.get_by_label("Effort").select_option("low")
    pop.get_by_label("Max rows").fill("250")
    pop.get_by_label("Max rows").press("Tab")
    expect(chip).to_have_text(re.compile(r"Claude Sonnet 5\.5 · low · ≤ 250 rows"))
    page.keyboard.press("Escape")
    expect(pop).to_be_hidden()

    # Changing the model gives it an Effort it offers; a model with none shows "no effort".
    chip.click()
    pop.get_by_label("Model").select_option("no-model")
    expect(chip).to_contain_text("No model · no effort · ≤ 250 rows")
    expect(pop.get_by_label("Effort")).to_be_disabled()
    pop.get_by_label("Model").select_option("anthropic/claude-sonnet-5.5")
    expect(chip).to_contain_text("Claude Sonnet 5.5 · high · ≤ 250 rows")
    pop.get_by_label("Effort").select_option("low")

    write_prompt(cell, "go")
    run(cell)
    expect(cell.locator(".cell-foot")).to_contain_text("with Claude Sonnet 5.5 · low · ≤ 250 rows")
    page.reload()  # the Draft survives a reload
    expect(page.locator(".cellblock .chip")).to_contain_text("Claude Sonnet 5.5 · low · ≤ 250 rows")


def test_a_new_ai_cell_starts_with_the_last_settings_run(live_page, build_agent):
    page = live_page(agent=build_agent(ScriptedLLM([TextChunk("done")])))
    cell = open_new_notebook(page)
    cell.locator(".chip").click()
    pop = cell.get_by_role("dialog", name="AI Cell settings")
    pop.get_by_label("Model").select_option("anthropic/claude-opus-5.5")
    pop.get_by_label("Effort").select_option("medium")
    pop.get_by_label("Max rows").fill("42")
    pop.get_by_label("Max rows").press("Tab")
    write_prompt(cell, "go")
    run(cell)
    expect(cell.locator(".gutter .num")).to_have_text("[1]")

    cell.locator("+ .add-bar").get_by_role("button", name="+ AI").click()
    expect(page.locator(".cellblock")).to_have_count(2)
    second = page.locator(".cellblock").nth(1)
    expect(second.locator(".chip")).to_contain_text("Claude Opus 5.5 · medium · ≤ 42 rows")
    # ... and the notebook created next starts there too
    page.get_by_role("button", name="New notebook").click()
    expect(page.locator(".cellblock .chip")).to_contain_text("Claude Opus 5.5 · medium · ≤ 42 rows")


def test_a_cell_added_between_two_lands_between_them_and_can_run(live_page, build_agent):
    page = live_page(agent=build_agent(ScriptedLLM([TextChunk("middle answer")])))
    first = open_new_notebook(page)
    first.locator("+ .add-bar").get_by_role("button", name="+ AI").click()
    expect(page.locator(".cellblock")).to_have_count(2)
    write_prompt(page.locator(".cellblock").nth(1), "last")
    first.locator("+ .add-bar").get_by_role("button", name="+ AI").click()  # between the two
    expect(page.locator(".cellblock")).to_have_count(3)
    middle = page.locator(".cellblock").nth(1)
    expect(middle.get_by_label("Prompt")).to_have_value("")
    expect(page.locator(".cellblock").nth(2).get_by_label("Prompt")).to_have_value("last")
    write_prompt(middle, "middle")
    run(middle)
    expect(middle.locator(".output")).to_contain_text("middle answer")
    expect(middle.locator(".gutter .num")).to_have_text("[1]")
    expect(page.locator(".cellblock .gutter .num")).to_have_text(["[ ]", "[1]", "[ ]"])
