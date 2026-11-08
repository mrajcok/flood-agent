"""A turn and a finished Trace slide and fade as they collapse and expand, instead of snapping."""

from conftest import ScriptedLLM
from playwright.sync_api import expect

from flood_agent.llm import TextChunk

# Heights and opacity of the details' content, sampled while it closes or opens.
SAMPLE = """
async ([selector, opening]) => {
  const details = document.querySelector(selector);
  details.open = !opening;
  await new Promise(r => setTimeout(r, 900));  // settle in the starting state
  details.open = opening;
  const seen = [];
  for (let i = 0; i < 12; i++) {
    await new Promise(r => setTimeout(r, 40));
    const content = getComputedStyle(details, '::details-content');
    seen.push({ height: parseFloat(content.height) || 0, opacity: parseFloat(content.opacity) });
  }
  return seen;
}
"""


def finished_turn(live_page, build_agent):
    page = live_page(agent=build_agent(ScriptedLLM([TextChunk("all done")])))
    page.get_by_placeholder("Write a prompt").fill("hello")
    page.get_by_role("button", name="Send message (Enter)").click()
    expect(page.locator("details.trace")).to_have_count(1)  # the turn is finished
    return page


def assert_slides_and_fades(seen):
    heights = [s["height"] for s in seen]
    opacities = [s["opacity"] for s in seen]
    assert any(0 < h < max(heights) for h in heights), heights  # in between, so it slid
    assert any(0 < o < 1 for o in opacities), opacities  # in between, so it faded


def test_a_turn_slides_and_fades_closed_and_open(live_page, build_agent):
    page = finished_turn(live_page, build_agent)
    assert_slides_and_fades(page.evaluate(SAMPLE, ["details.turn", False]))
    assert_slides_and_fades(page.evaluate(SAMPLE, ["details.turn", True]))


def test_a_finished_trace_slides_and_fades_closed_and_open(live_page, build_agent):
    page = finished_turn(live_page, build_agent)
    assert_slides_and_fades(page.evaluate(SAMPLE, ["details.trace", True]))
    assert_slides_and_fades(page.evaluate(SAMPLE, ["details.trace", False]))
