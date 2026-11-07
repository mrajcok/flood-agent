"""The Send button turns into Stop while a turn runs; the running turn shows its LLM calls live."""

import asyncio
import threading

from conftest import ScriptedLLM, tool_call
from playwright.sync_api import expect
from test_stop_turn import SQL, HangingLLM

from flood_agent.llm import TextChunk


class GatedLLM(ScriptedLLM):
    """Waits for `gate` before its second call, so a test can look at the page mid-turn."""

    def __init__(self, *steps):
        super().__init__(*steps)
        self.gate = threading.Event()

    async def stream_chat(self, messages, tools, reasoning=None, session_id=None):
        if self.calls:
            while not self.gate.is_set():
                await asyncio.sleep(0.01)
        async for chunk in super().stream_chat(messages, tools, reasoning, session_id):
            yield chunk


def send(page, text="show me ports"):
    page.get_by_placeholder("Write a prompt").fill(text)
    page.get_by_role("button", name="Send message (Enter)").click()


def test_clicking_stop_ends_the_turn_keeping_its_text_and_restores_send(live_page, build_agent):
    page = live_page(agent=build_agent(HangingLLM()))
    send(page)
    stop = page.get_by_role("button", name="Stop")
    expect(stop).to_be_visible()
    expect(page.get_by_role("button", name="Send message (Enter)")).to_be_hidden()
    expect(page.locator(".thinking")).to_have_count(0)
    turn = page.locator("details.turn").last
    expect(turn.locator(".live-step")).to_have_count(1)
    expect(turn).to_contain_text("Partial answer")

    stop.click()
    expect(turn.locator("details.trace")).to_have_count(1)
    expect(turn.locator("details.trace > summary")).to_contain_text("stopped")
    expect(turn.locator(".live-trace")).to_have_count(0)
    expect(turn).to_contain_text("Stopped by user.")
    expect(turn).to_contain_text("Partial answer")
    expect(page.get_by_role("button", name="Send message (Enter)")).to_be_visible()
    expect(page.get_by_role("button", name="Stop")).to_be_hidden()


def test_enter_does_nothing_while_a_turn_runs(live_page, build_agent):
    page = live_page(agent=build_agent(HangingLLM()))
    send(page)
    expect(page.get_by_role("button", name="Stop")).to_be_visible()
    box = page.get_by_placeholder("Write a prompt")
    box.fill("second prompt")
    box.press("Enter")
    assert box.input_value() == "second prompt"
    expect(page.locator("details.turn")).to_have_count(1)


def test_each_llm_call_collapses_when_the_next_starts_and_all_fold_into_the_trace_line(live_page, build_agent):
    llm = GatedLLM([TextChunk("Looking."), tool_call("run_query", sql=SQL, title="t")], [TextChunk("Done.")])
    page = live_page(agent=build_agent(llm))
    send(page)
    turn = page.locator("details.turn").last
    steps = turn.locator(".live-step")
    expect(steps).to_have_count(2)
    expect(steps.nth(0)).not_to_have_attribute("open", "")
    expect(steps.nth(1)).to_have_attribute("open", "")
    expect(steps.nth(0).locator("summary")).to_contain_text("1 tool call")
    steps.nth(0).locator("summary").click()  # a collapsed call can be opened to read
    expect(steps.nth(0)).to_contain_text(SQL)

    llm.gate.set()
    expect(turn.locator("details.trace")).to_have_count(1)
    expect(turn.locator(".live-trace")).to_have_count(0)
    expect(turn.locator("details.trace > summary")).not_to_contain_text("stopped")
    expect(page.get_by_role("button", name="Send message (Enter)")).to_be_visible()
