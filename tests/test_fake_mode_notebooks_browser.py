"""The Canned Prompt picker in an AI Cell: shown in place of the prompt box under "No model"."""

from conftest import ScriptedLLM
from playwright.sync_api import expect
from test_notebook_browser import open_new_notebook

from flood_agent.__main__ import picker_entries
from flood_agent.agent import NO_MODEL
from flood_agent.fake_llm import FakeClient, load_canned_prompts, parent_of

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

    page.get_by_role("button", name="+ AI").last.click()  # inherits the last settings: No model
    second = page.locator(".cellblock.ai").nth(1)
    expect(second.get_by_label(PICKER)).to_be_visible()
    pick(second, PARENT.prompt)
    expect(second.locator(".gutter .num")).to_have_text("[2]")

    page.get_by_role("button", name="+ AI").last.click()
    third = page.locator(".cellblock.ai").nth(2)
    pick(third, FOLLOW.prompt)
    expect(third.locator(".gutter .num")).to_have_text("[3]")
    expect(third.locator(".output")).not_to_contain_text("first in an AI Cell above")
