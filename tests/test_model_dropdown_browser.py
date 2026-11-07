"""The strip's Model dropdown: the curated list, the remembered choice and its fallback."""

import asyncio
import threading

from conftest import ScriptedLLM
from playwright.sync_api import expect

from flood_agent.llm import TextChunk, UsageChunk

LABELS = [
    "Claude Sonnet 5.5", "Claude Opus 5.5", "Claude Haiku 5.5", "DeepSeek V4 Pro", "GLM 5.3", "Qwen3.8 27B",
    "GPT-5.6 Terra", "GPT-5.6 Sol", "GPT-5.6 Luna",
]


def open_model_panel(page):
    page.locator("button[title='Model for the next prompt']").click()
    return page.get_by_role("dialog", name="Model")


def trigger(page):
    return page.locator("button[title='Model for the next prompt']")


def test_the_dropdown_lists_the_curated_models_in_order_with_the_default_preselected(live_page):
    page = live_page()
    expect(trigger(page)).to_have_text("GLM 5.3")
    expect(page.locator("#model")).to_have_value("z-ai/glm-5.3")

    panel = open_model_panel(page)

    expect(panel.get_by_role("heading")).to_have_text("Model")
    expect(panel.locator(".item > span:nth-child(2)")).to_have_text([*LABELS, "No model"])
    expect(panel.get_by_role("button", name="GLM 5.3")).to_be_enabled()


def test_the_choice_is_what_gets_sent_and_survives_a_reload(build_agent, live_page):
    page = live_page(agent=build_agent(ScriptedLLM([TextChunk("hi")])))
    open_model_panel(page).get_by_role("button", name="Claude Opus 5.5").click()
    expect(trigger(page)).to_have_text("Claude Opus 5.5")
    expect(page.locator("#model")).to_have_value("anthropic/claude-opus-5.5")

    page.locator("textarea[name=message]").fill("hello")
    page.locator("textarea[name=message]").press("Enter")

    expect(page.locator("#chat")).to_contain_text("Opus-5.5")  # the Trace shows the model that ran
    page.reload()
    expect(trigger(page)).to_have_text("Claude Opus 5.5")


def test_a_remembered_model_that_no_longer_exists_falls_back_to_the_default(live_page):
    page = live_page()
    page.evaluate("localStorage.setItem('flood-agent.model', 'gone/model')")
    page.reload()

    expect(trigger(page)).to_have_text("GLM 5.3")
    expect(page.locator("#model")).to_have_value("z-ai/glm-5.3")


def test_the_meter_follows_the_selected_models_window(live_page):
    page = live_page()
    meter = page.locator("#context-meter")
    expect(meter).to_contain_text("0 tokens of 1.05M")

    open_model_panel(page).get_by_role("button", name="Qwen3.8 27B").click()
    expect(meter).to_contain_text("0 tokens of 1.02M")

    open_model_panel(page).get_by_role("button", name="Claude Sonnet 5.5").click()
    expect(meter).to_contain_text("0 tokens of 1M")
    expect(page.locator("#context-meter")).to_have_count(1)


def test_with_no_key_real_models_are_disabled_and_a_remembered_one_falls_back(live_page):
    page = live_page(has_api_key=False)
    page.evaluate("localStorage.setItem('flood-agent.model', 'anthropic/claude-opus-5.5')")
    page.reload()

    panel = open_model_panel(page)

    for label in LABELS:
        expect(panel.get_by_role("button", name=label)).to_be_disabled()
    expect(panel.get_by_role("button", name="No model")).to_be_enabled()
    expect(panel).to_contain_text("needs OPENROUTER_API_KEY")
    expect(page.locator("#model")).to_have_value("no-model")


class GatedLLM:
    """Answers once `gate` is set, so a test can change the dropdown while the turn is still running."""

    def __init__(self, gate: threading.Event):
        self.gate = gate

    async def stream_chat(self, messages, tools, reasoning: dict | None = None, session_id: str | None = None):
        while not self.gate.is_set():
            await asyncio.sleep(0.02)
        yield TextChunk("hi")
        yield UsageChunk(prompt_tokens=100, completion_tokens=20, cost=0.0012)


def test_after_a_turn_the_meter_shows_the_window_of_the_model_selected_for_the_next_prompt(build_agent, live_page):
    gate = threading.Event()
    page = live_page(agent=build_agent(GatedLLM(gate)))
    open_model_panel(page).get_by_role("button", name="Claude Sonnet 5.5").click()  # 1M window
    page.locator("textarea[name=message]").fill("hello")
    page.locator("textarea[name=message]").press("Enter")
    open_model_panel(page).get_by_role("button", name="Qwen3.8 27B").click()  # 1.02M window, mid-turn
    gate.set()

    expect(page.locator("#chat")).to_contain_text("Sonnet-5.5")  # the Trace: Sonnet ran
    expect(page.locator("#context-meter")).to_contain_text("100 tokens of 1.02M")
