"""Copy to Markdown Cell and embedded Displays in a real browser."""

from conftest import ScriptedLLM
from playwright.sync_api import expect
from test_markdown_displays import answer_with_table_and_chart


def ran_page(live_page, build_agent):
    page = live_page(agent=build_agent(ScriptedLLM(*answer_with_table_and_chart())))
    page.get_by_role("button", name="New notebook").click()
    page.get_by_placeholder("Write a prompt").fill("top ports")
    page.get_by_role("button", name="Run").click()
    expect(page.locator(".cellblock.ai .display.chart").first).to_be_visible()
    expect(page.get_by_role("button", name="Copy to Markdown Cell")).to_be_visible()
    return page


def test_copy_offers_to_save_then_embeds_live_displays_that_follow_the_rename(live_page, build_agent):
    page = ran_page(live_page, build_agent)
    page.get_by_role("button", name="Copy to Markdown Cell").click()
    offer = page.get_by_role("group", name="Save tables")
    expect(offer).to_be_visible()
    offer.locator("input").fill("top_ports")
    offer.get_by_role("button", name="Copy").click()

    md = page.locator(".cellblock.markdown")
    expect(md.get_by_text("Port 8443 moved most.")).to_be_visible()
    expect(md.get_by_label("Send to AI")).not_to_be_checked()
    expect(md.locator(".display table")).to_contain_text("8443")
    expect(md.locator(".display.chart canvas, .display.chart svg.marks").first).to_be_visible()
    expect(md.locator(".table-name .name").first).to_have_text("top_ports")

    page.reload()
    expect(page.locator(".cellblock.markdown .display table")).to_contain_text("8443")
    expect(page.locator(".cellblock.ai .table-name .name").first).to_have_text("top_ports")  # the AI Cell followed too


def test_a_deleted_table_shows_expired_in_the_embedded_display(live_page, build_agent):
    page = ran_page(live_page, build_agent)
    page.get_by_role("button", name="Copy to Markdown Cell").click()
    page.get_by_role("group", name="Save tables").get_by_role("button", name="Copy").click()
    md = page.locator(".cellblock.markdown")
    expect(md.locator(".display table")).to_contain_text("8443")
    first = md.locator(".display").first
    first.get_by_role("button", name="Delete rt_", exact=False).first.click()
    first.get_by_role("button", name="Delete", exact=True).click()
    expect(md.get_by_text("expired").first).to_be_visible()
    page.reload()
    expect(page.locator(".cellblock.markdown").get_by_text("expired").first).to_be_visible()
