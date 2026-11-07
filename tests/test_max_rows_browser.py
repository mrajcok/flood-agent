"""The strip under the prompt box: model and context on the left, the row cap on the right."""

from conftest import ScriptedLLM
from playwright.sync_api import expect

from flood_agent.llm import TextChunk


def test_strip_sits_beneath_the_prompt_box_and_the_row_cap_survives_sending(live_page):
    page = live_page()
    box = page.locator("textarea[name=message]")
    trigger = page.get_by_role("button", name="rows per query")
    expect(trigger).to_have_text("≤ 10,000 rows per query")
    assert box.bounding_box()["y"] < trigger.bounding_box()["y"]

    trigger.click()
    dialog = page.get_by_role("dialog", name="Max rows kept per query")
    expect(dialog.get_by_role("heading")).to_have_text("Max rows kept per query")
    dialog.get_by_role("button", name="100", exact=True).click()
    expect(dialog).to_be_hidden()
    expect(trigger).to_have_text("≤ 100 rows per query")

    box.fill("hello")
    box.press("Enter")
    expect(page.locator("#chat details.turn")).to_have_count(1)
    expect(trigger).to_have_text("≤ 100 rows per query")
    expect(page.locator("#max_rows")).to_have_value("100")


def test_a_custom_row_cap_is_what_gets_sent(live_page):
    page = live_page()
    page.get_by_role("button", name="rows per query").click()
    page.get_by_label("Custom max rows").fill("250")
    expect(page.locator("#max_rows")).to_have_value("250")
    expect(page.get_by_role("button", name="rows per query")).to_have_text("≤ 250 rows per query")


def test_the_context_meter_updates_in_place_when_a_turn_ends(build_agent, live_page):
    page = live_page(agent=build_agent(ScriptedLLM([TextChunk("hi")])))
    meter = page.locator("#context-meter")
    expect(meter).to_contain_text("0 tokens of 1.05M")
    page.locator("textarea[name=message]").fill("hello")
    page.locator("textarea[name=message]").press("Enter")
    expect(meter).to_contain_text("100 tokens of 1.05M")
    expect(page.locator("#context-meter")).to_have_count(1)
