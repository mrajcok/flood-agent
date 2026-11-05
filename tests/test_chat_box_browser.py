"""The chat box: grows as you type, Enter sends, Shift+Enter adds a line, a send icon inside it."""

from conftest import ScriptedLLM
from playwright.sync_api import expect

from flood_agent.llm import TextChunk


def replies(n):
    return ScriptedLLM(*([TextChunk("ok")] for _ in range(n)))


def test_there_is_no_ask_button_but_a_send_icon_inside_the_box_with_a_tooltip(live_page, build_agent):
    page = live_page(agent=build_agent(replies(0)))
    expect(page.get_by_role("button", name="Ask")).to_have_count(0)
    send = page.get_by_role("button", name="Send message (Enter)")
    expect(send).to_have_attribute("title", "Send message (Enter)")
    box = page.get_by_placeholder("Write a prompt").bounding_box()
    icon = send.bounding_box()
    assert box and icon
    assert box["x"] <= icon["x"] and icon["x"] + icon["width"] <= box["x"] + box["width"]
    assert box["y"] <= icon["y"] and icon["y"] + icon["height"] <= box["y"] + box["height"]


def test_shift_enter_adds_a_line_and_the_box_grows_then_enter_sends_it_all(live_page, build_agent):
    page = live_page(agent=build_agent(replies(1)))
    chat = page.get_by_placeholder("Write a prompt")
    one_row = chat.bounding_box()["height"]  # type: ignore[index]
    chat.press_sequentially("top ports")
    chat.press("Shift+Enter")
    chat.press_sequentially("last hour")
    chat.press("Shift+Enter")
    chat.press_sequentially("by circuit")
    expect(chat).to_have_value("top ports\nlast hour\nby circuit")
    assert chat.bounding_box()["height"] > 2 * one_row  # type: ignore[index]
    expect(page.locator(".user")).to_have_count(0)  # nothing sent yet

    chat.press("Enter")
    expect(page.locator(".user").first).to_have_text("top ports\nlast hour\nby circuit")
    expect(chat).to_have_value("")
    expect(chat).to_have_js_property("offsetHeight", round(one_row))  # shrinks back to one row


def test_the_box_grows_for_long_wrapped_text_and_typing_into_it_works(live_page, build_agent):
    page = live_page(agent=build_agent(replies(0)))
    chat = page.get_by_placeholder("Write a prompt")
    one_row = chat.bounding_box()["height"]  # type: ignore[index]
    chat.press_sequentially("bytes per circuit " * 12)
    assert chat.bounding_box()["height"] > one_row  # type: ignore[index]


def test_enter_on_an_empty_box_sends_nothing_and_the_icon_sends_too(live_page, build_agent):
    page = live_page(agent=build_agent(replies(1)))
    chat = page.get_by_placeholder("Write a prompt")
    chat.press("Enter")
    page.wait_for_timeout(300)
    expect(page.locator(".user")).to_have_count(0)
    chat.fill("top ports")
    page.get_by_role("button", name="Send message (Enter)").click()
    expect(page.locator(".user").first).to_have_text("top ports")


def test_a_prompt_of_only_blank_lines_is_not_sent_and_the_icon_returns_focus_to_the_box(live_page, build_agent):
    page = live_page(agent=build_agent(replies(1)))
    chat = page.get_by_placeholder("Write a prompt")
    chat.press("Shift+Enter")
    chat.press_sequentially("  ")
    chat.press("Enter")
    page.get_by_role("button", name="Send message (Enter)").click()
    page.wait_for_timeout(300)
    expect(page.locator(".user")).to_have_count(0)
    chat.fill("top ports")
    page.get_by_role("button", name="Send message (Enter)").click()
    expect(page.locator(".user").first).to_have_text("top ports")
    expect(chat).to_be_focused()


def test_pressing_escape_twice_clears_the_box_but_once_or_slowly_does_not(live_page, build_agent):
    page = live_page(agent=build_agent(replies(0)))
    chat = page.get_by_placeholder("Write a prompt")
    one_row = chat.bounding_box()["height"]  # type: ignore[index]
    chat.fill("top ports\nlast hour\nby circuit")
    chat.dispatch_event("input")
    chat.press("Escape")
    expect(chat).to_have_value("top ports\nlast hour\nby circuit")
    page.wait_for_timeout(1000)  # too slow for a double press
    chat.press("Escape")
    expect(chat).to_have_value("top ports\nlast hour\nby circuit")
    chat.press("Escape")
    expect(chat).to_have_value("")
    expect(chat).to_be_focused()
    expect(chat).to_have_js_property("offsetHeight", round(one_row))  # back to one row


def test_the_collapse_button_toggles_between_collapse_turns_and_expand_turns(live_page, build_agent):
    page = live_page(agent=build_agent(replies(1)))
    chat = page.get_by_placeholder("Write a prompt")
    chat.fill("top ports")
    chat.press("Enter")
    expect(page.locator(".reply", has_text="ok")).to_be_visible()
    page.get_by_role("button", name="Collapse turns").click()
    expect(page.locator(".reply", has_text="ok")).to_be_hidden()
    page.get_by_role("button", name="Expand turns").click()
    expect(page.locator(".reply", has_text="ok")).to_be_visible()
