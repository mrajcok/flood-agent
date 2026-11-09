"""Readline-style editing keys in the prompt box, driven in headless system Chrome."""

import pytest
from conftest import ScriptedLLM
from playwright.sync_api import expect
from test_stop_turn import HangingLLM

from flood_agent.llm import TextChunk


def box_with(live_page, text, cursor):
    """The prompt box holding `text` with the cursor at offset `cursor`."""
    page = live_page()
    box = page.get_by_placeholder("Write a prompt")
    box.fill(text)
    box.evaluate("(el, c) => el.setSelectionRange(c, c)", cursor)
    return page, box


def state(box):
    return box.evaluate("el => [el.value, el.selectionStart]")


@pytest.mark.parametrize(
    "text, cursor, key, value, pos",
    [
        ("hello world", 5, "Control+a", "hello world", 0),
        ("hello world", 5, "Control+e", "hello world", 11),
        ("one\ntwo three", 8, "Control+a", "one\ntwo three", 4),  # start of this line
        ("one\ntwo three", 5, "Control+e", "one\ntwo three", 13),
        ("hello", 2, "Control+b", "hello", 1),
        ("hello", 2, "Control+f", "hello", 3),
        ("hello big world", 7, "Alt+b", "hello big world", 6),
        ("hello big world", 7, "Alt+f", "hello big world", 9),
        ("hello", 3, "Control+h", "helo", 2),
        ("hello", 1, "Control+d", "hllo", 1),
        ("hello world", 5, "Control+k", "hello", 5),
        ("one\ntwo", 1, "Control+k", "o\ntwo", 1),  # only to the end of the line
        ("one\ntwo", 3, "Control+k", "onetwo", 3),  # at the end of a line it joins the next
        ("hello world", 5, "Control+u", " world", 0),
        ("one\ntwo", 6, "Control+u", "one\no", 4),
        ("hello big world", 6, "Alt+d", "hello  world", 6),
        ("abcd", 2, "Control+t", "acbd", 3),
        ("abcd", 4, "Control+t", "abdc", 4),
    ],
)
def test_editing_keys(live_page, text, cursor, key, value, pos):
    _, box = box_with(live_page, text, cursor)
    box.press(key)
    assert state(box) == [value, pos]


def test_ctrl_y_yanks_what_ctrl_k_or_ctrl_u_killed(live_page):
    _, box = box_with(live_page, "hello world", 5)
    box.press("Control+k")
    box.press("Control+a")
    box.press("Control+y")
    assert state(box) == [" worldhello", 6]


def test_ctrl_a_with_a_selection_still_selects_all(live_page):
    _, box = box_with(live_page, "hello", 2)
    box.press("Shift+ArrowRight")
    box.press("Control+a")
    assert box.evaluate("el => [el.selectionStart, el.selectionEnd]") == [0, 5]


def test_an_edit_can_be_undone(live_page):
    _, box = box_with(live_page, "hello world", 5)
    box.press("Control+k")
    box.press("Control+z")
    assert state(box)[0] == "hello world"


def test_the_box_still_grows_after_a_kill(live_page):
    page, box = box_with(live_page, "a\nb\nc\nd", 0)
    tall = box.evaluate("el => el.offsetHeight")
    box.press("Control+u")
    box.press("Control+e")
    for _ in range(3):
        box.press("Control+k")
    expect(box).to_be_visible()
    assert box.evaluate("el => el.offsetHeight") <= tall


def test_enter_still_sends_and_ctrl_w_is_left_alone(live_page):
    _, box = box_with(live_page, "hello big", 9)
    box.press("Control+w")  # the browser's, not ours: the text must not change
    assert state(box)[0] == "hello big"


def send_prompts(page, *prompts):
    box = page.get_by_placeholder("Write a prompt")
    for p in prompts:
        box.fill(p)
        page.get_by_role("button", name="Send message (Enter)").click()
        expect(page.locator("summary.user", has_text=p)).to_be_visible()
        expect(page.get_by_role("button", name="Send message (Enter)")).to_be_visible()
    return box


def test_up_and_down_walk_through_earlier_prompts_and_back_to_the_draft(live_page, build_agent):
    page = live_page(agent=build_agent(ScriptedLLM(*[[TextChunk("ok")]] * 3)))
    box = send_prompts(page, "first", "second", "third")
    box.fill("draft")
    box.evaluate("el => el.setSelectionRange(0, 0)")
    for want in ("third", "second", "first", "first"):  # stops at the oldest
        box.press("ArrowUp")
        assert box.input_value() == want
    for want in ("second", "third", "draft", "draft"):  # back down to what was being typed
        box.press("ArrowDown")
        assert box.input_value() == want


def test_up_only_recalls_from_the_first_line_and_down_from_the_last(live_page, build_agent):
    page = live_page(agent=build_agent(ScriptedLLM([TextChunk("ok")])))
    box = send_prompts(page, "earlier")
    box.fill("one\ntwo")  # cursor at the end, on the second line
    box.press("ArrowUp")  # moves to line one, no recall
    assert box.input_value() == "one\ntwo"
    box.press("ArrowUp")  # now on the first line
    assert box.input_value() == "earlier"


def test_up_recalls_the_prompt_after_escape_stops_a_turn(live_page, build_agent):
    page = live_page(agent=build_agent(HangingLLM()))
    box = page.get_by_placeholder("Write a prompt")
    box.fill("my mistaken prompt")
    page.get_by_role("button", name="Send message (Enter)").click()
    expect(page.get_by_role("button", name="Stop")).to_be_visible()
    page.keyboard.press("Escape")
    expect(page.get_by_role("button", name="Send message (Enter)")).to_be_visible()
    box.focus()
    box.press("ArrowUp")
    assert box.input_value() == "my mistaken prompt"


def test_up_with_no_earlier_prompts_does_nothing(live_page):
    page = live_page()
    box = page.get_by_placeholder("Write a prompt")
    box.fill("draft")
    box.evaluate("el => el.setSelectionRange(0, 0)")
    box.press("ArrowUp")
    assert box.input_value() == "draft"


def test_sending_a_recalled_prompt_starts_the_walk_again_from_the_newest(live_page, build_agent):
    page = live_page(agent=build_agent(ScriptedLLM(*[[TextChunk("ok")]] * 3)))
    box = send_prompts(page, "first", "second")
    box.press("ArrowUp")
    box.press("ArrowUp")
    assert box.input_value() == "first"
    page.get_by_role("button", name="Send message (Enter)").click()
    expect(page.locator("summary.user", has_text="first")).to_have_count(2)
    expect(page.get_by_role("button", name="Send message (Enter)")).to_be_visible()
    box.focus()
    box.press("ArrowUp")
    assert box.input_value() == "first"  # the newest prompt is the one just resent
    box.press("ArrowUp")
    assert box.input_value() == "second"
