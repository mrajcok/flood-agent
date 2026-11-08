"""Opening another conversation: its start is shown, the chat pane cross-fades, and sending a prompt follows the new turn."""

from conftest import ScriptedLLM
from playwright.sync_api import expect

from flood_agent.llm import TextChunk

LONG = "\n\n".join(f"Paragraph {n} of a long answer." for n in range(40))
PANE = "document.getElementById('prompt-area')"


def send(page, text, turns):
    page.get_by_placeholder("Write a prompt").fill(text)
    page.get_by_role("button", name="Send message (Enter)").click()
    expect(page.locator("details.trace")).to_have_count(turns)


def long_conversation_then_reopen(live_page, build_agent, extra_replies=()):
    llm = ScriptedLLM([TextChunk(LONG)], [TextChunk(LONG)], *extra_replies)
    page = live_page(agent=build_agent(llm))
    send(page, "first question", 1)
    send(page, "second question", 2)
    page.get_by_role("link", name="New chat").click()
    page.locator("#sidebar a[href^='/c/']").first.click()
    expect(page.locator("details.turn")).to_have_count(2)
    page.wait_for_timeout(300)  # let any scroll settle
    return page


def test_an_opened_conversation_shows_its_start(live_page, build_agent):
    page = long_conversation_then_reopen(live_page, build_agent)
    assert page.evaluate(f"{PANE}.scrollHeight > {PANE}.clientHeight")  # long enough to scroll
    assert page.evaluate(f"{PANE}.scrollTop") == 0
    expect(page.locator("details.turn summary").first).to_be_in_viewport()


def test_sending_a_prompt_in_an_opened_conversation_scrolls_to_the_new_turn(live_page, build_agent):
    page = long_conversation_then_reopen(live_page, build_agent, extra_replies=([TextChunk("third answer")],))
    send(page, "third question", 3)
    expect(page.locator(".reply", has_text="third answer")).to_be_in_viewport()


def test_switching_conversation_runs_a_view_transition(live_page, build_agent):
    page = live_page(agent=build_agent(ScriptedLLM([TextChunk("hello")])))
    send(page, "hi", 1)
    page.get_by_role("link", name="New chat").click()
    page.add_init_script("window.__transitions = []; addEventListener('pagereveal', e => window.__transitions.push(!!e.viewTransition));")
    page.locator("#sidebar a[href^='/c/']").first.click()
    expect(page.locator("details.turn")).to_have_count(1)
    assert page.evaluate("window.__transitions") == [True]
