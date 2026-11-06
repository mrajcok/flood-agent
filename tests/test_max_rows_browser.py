"""The row-cap control sits under the prompt box, worded as a sentence."""

from playwright.sync_api import expect


def test_row_cap_sits_beneath_the_prompt_box_and_survives_sending(live_page):
    page = live_page()
    box = page.locator("textarea[name=message]")
    control = page.locator(".max-rows")
    expect(control).to_contain_text("Any queries that are executed by this prompt will be truncated to")
    assert box.bounding_box()["y"] < control.bounding_box()["y"]
    page.locator("#max-rows-choice").select_option("100")
    box.fill("hello")
    box.press("Enter")
    expect(page.locator("#chat details.turn")).to_have_count(1)
    expect(page.locator("#max-rows-choice")).to_have_value("100")
    expect(page.locator("#max_rows")).to_have_value("100")
