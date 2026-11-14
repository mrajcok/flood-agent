"""The notebook list in the sidebar."""

from conftest import ScriptedLLM
from playwright.sync_api import expect


def test_a_cut_off_notebook_name_shows_in_full_in_a_tooltip_on_hover(live_page, build_agent):
    agent = build_agent(ScriptedLLM())
    long = "Investigating the volumetric attack on circuit ABCDEF123456"  # 59: titles are capped at 60
    agent.rename_notebook(agent.create_notebook(), long)
    agent.rename_notebook(agent.create_notebook(), "Short")
    page = live_page(agent=agent)

    cut, short = page.locator(".notebook-list li a", has_text="Investigating"), page.locator(".notebook-list li a", has_text="Short")
    assert cut.evaluate("a => a.scrollWidth > a.clientWidth")  # sanity: it is cut off
    cut.hover()
    expect(cut).to_have_attribute("title", long)
    short.hover()
    expect(short).not_to_have_attribute("title", "Short")


def test_the_cells_fill_the_width_beside_an_expanded_sidebar(live_page):
    page = live_page()
    page.set_viewport_size({"width": 1800, "height": 800})
    page.goto(page.url)
    cells = page.locator("main").bounding_box()
    assert cells
    assert cells["x"] + cells["width"] >= 1800 - 40
