"""The Canned Prompt picker in an AI Cell: shown in place of the prompt box under "No model"."""

import re

from conftest import ScriptedLLM, add_below
from playwright.sync_api import expect
from test_notebook_browser import open_new_notebook, run, write_prompt

from flood_agent.__main__ import picker_entries
from flood_agent.agent import NO_MODEL
from flood_agent.fake_llm import FakeClient, load_canned_prompts, parent_of
from flood_agent.llm import TextChunk

CANNED = load_canned_prompts()
FOLLOW = next(c for c in CANNED if c.after)
PARENT = parent_of(FOLLOW, CANNED)
PICKER = "Fake Mode picker"


def fake_agent(build_agent):
    agent = build_agent(ScriptedLLM())
    fake = FakeClient(CANNED, chunk_delay_s=0)
    agent.client_for = lambda model: fake
    return agent


def choose_no_model(cell):
    cell.locator(".chip").click()
    cell.get_by_label("Model").select_option(NO_MODEL)
    cell.locator(".chip").click()  # close the popover


def pick(cell, prompt):
    cell.get_by_label(PICKER).click()
    cell.get_by_role("option", name=prompt).click()


def test_no_model_swaps_the_prompt_box_for_the_picker_and_picking_runs_it(live_page, build_agent):
    page = live_page(agent=fake_agent(build_agent), picker=picker_entries(CANNED))
    cell = open_new_notebook(page)
    expect(cell.get_by_label("Prompt")).to_be_visible()
    expect(cell.get_by_label(PICKER)).to_be_hidden()

    choose_no_model(cell)
    expect(cell.get_by_label("Prompt")).to_be_hidden()
    expect(cell.get_by_label(PICKER)).to_be_visible()

    pick(cell, CANNED[0].prompt)
    expect(cell.locator(".gutter .num")).to_have_text("[1]")
    expect(cell.locator(".output")).not_to_be_empty()
    expect(cell.locator(".cell-foot")).to_contain_text("No model")


def test_a_follow_up_runs_below_its_parent_and_asks_for_it_when_there_is_none_above(live_page, build_agent):
    page = live_page(agent=fake_agent(build_agent), picker=picker_entries(CANNED))
    first = open_new_notebook(page)
    choose_no_model(first)
    pick(first, FOLLOW.prompt)
    expect(first.locator(".output")).to_contain_text("first in an AI Cell above")
    expect(first.locator(".gutter .num")).to_have_text("[1]")  # the run has finished and its settings are saved

    add_below(page, "ai")  # inherits the last settings: No model
    second = page.locator(".cellblock.ai").nth(1)
    expect(second.get_by_label(PICKER)).to_be_visible()
    pick(second, PARENT.prompt)
    expect(second.locator(".gutter .num")).to_have_text("[2]")

    add_below(page, "ai")
    third = page.locator(".cellblock.ai").nth(2)
    pick(third, FOLLOW.prompt)
    expect(third.locator(".gutter .num")).to_have_text("[3]")
    expect(third.locator(".output")).not_to_contain_text("first in an AI Cell above")


def choose_model(cell, label_value):
    cell.locator(".chip").click()
    cell.get_by_label("Model").select_option(label_value)
    cell.locator(".chip").click()


def test_the_picker_filters_as_you_type_and_enter_runs_the_highlighted_prompt(live_page, build_agent):
    page = live_page(agent=fake_agent(build_agent), picker=picker_entries(CANNED))
    cell = open_new_notebook(page)
    choose_no_model(cell)
    find = cell.get_by_label(PICKER)
    find.click()
    expect(cell.get_by_role("option")).to_have_count(len(CANNED))
    find.fill(CANNED[0].prompt[:12].upper())  # matching ignores case
    options = cell.get_by_role("option")
    assert 1 <= options.count() < len(CANNED)
    expect(options.first).to_contain_text(CANNED[0].prompt)
    find.press("Enter")
    expect(cell.locator(".gutter .num")).to_have_text("[1]")
    expect(cell.get_by_role("option")).to_have_count(0)


def test_with_no_key_a_new_cell_starts_on_no_model_with_the_picker(live_page):
    page = live_page(has_api_key=False, picker=picker_entries(CANNED))
    cell = open_new_notebook(page)
    expect(cell.locator(".chip")).to_contain_text("No model")
    expect(cell.get_by_label(PICKER)).to_be_visible()


def test_one_notebook_mixes_fake_and_real_cells_each_footer_showing_its_model(live_page, build_agent):
    real = ScriptedLLM([TextChunk("real answer")])
    agent = build_agent(real)
    fake = FakeClient(CANNED, chunk_delay_s=0)
    agent.client_for = lambda model: fake if model == NO_MODEL else real
    page = live_page(agent=agent, picker=picker_entries(CANNED))
    first = open_new_notebook(page)
    choose_no_model(first)
    pick(first, CANNED[0].prompt)
    expect(first.locator(".cell-foot")).to_contain_text("No model")
    expect(first.locator(".cell-foot .ctx")).to_contain_text("no context used")

    add_below(page, "ai")
    second = page.locator(".cellblock.ai").nth(1)
    choose_model(second, "anthropic/claude-opus-5.5")
    write_prompt(second, "now for real")
    run(second)
    expect(second.locator(".cell-foot")).to_contain_text("Claude Opus 5.5")
    expect(second.locator(".cell-foot .ctx")).to_contain_text("100 tokens of 1M")


def test_the_footer_shows_cached_and_cache_write_tokens_only_when_some_were(live_page, build_agent):
    for attr, tokens, text in (("cached_tokens", 80, "80 cached"), ("cache_write_tokens", 70, "70 cache write")):
        llm = ScriptedLLM([TextChunk("first")])
        setattr(llm, attr, tokens)
        page = live_page(agent=build_agent(llm))
        cell = open_new_notebook(page)
        write_prompt(cell, "hello")
        run(cell)
        expect(cell.locator(".cell-foot")).to_contain_text("1 LLM call")
        cell.locator("details.cell-trace > summary").click()
        expect(cell.locator(".trace-llm_step h4")).to_contain_text(text)

    plain = live_page(agent=build_agent(ScriptedLLM([TextChunk("second")])))
    cell = open_new_notebook(plain)
    write_prompt(cell, "hello")
    run(cell)
    expect(cell.locator(".cell-foot")).to_contain_text("1 LLM call")
    cell.locator("details.cell-trace > summary").click()
    expect(cell.locator(".trace-llm_step h4")).to_be_visible()
    expect(cell.locator(".cell-trace")).not_to_contain_text(re.compile("cached|cache write"))
