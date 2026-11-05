"""The conversation list in the sidebar."""

from conftest import ScriptedLLM
from playwright.sync_api import expect


def test_a_cut_off_conversation_name_shows_in_full_in_a_tooltip_on_hover(live_page, build_agent):
    agent = build_agent(ScriptedLLM())
    long = "Investigating the volumetric attack on circuit ABCDEF123456"  # 59: titles are capped at 60
    agent.store.rename_conversation(agent.store.create_conversation(), long)
    agent.store.rename_conversation(agent.store.create_conversation(), "Short")
    page = live_page(agent=agent)

    cut, short = page.locator("aside ul li a", has_text="Investigating"), page.locator("aside ul li a", has_text="Short")
    assert cut.evaluate("a => a.scrollWidth > a.clientWidth")  # sanity: it is cut off
    cut.hover()
    expect(cut).to_have_attribute("title", long)
    short.hover()
    expect(short).not_to_have_attribute("title", "Short")
