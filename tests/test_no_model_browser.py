"""\"No model\" in the browser: Canned Prompts show only under it, and fake and real turns mix in one Conversation."""

from conftest import ScriptedLLM
from playwright.sync_api import expect

from flood_agent.__main__ import picker_entries
from flood_agent.agent import NO_MODEL
from flood_agent.fake_llm import FakeClient, load_canned_prompts
from flood_agent.llm import TextChunk

CANNED = load_canned_prompts()
FIRST = CANNED[0].prompt
TRIGGER = "button[title='Model for the next prompt']"


def choose(page, label):
    page.locator(TRIGGER).click()
    page.get_by_role("dialog", name="Model").get_by_role("button", name=label).click()


def agent_with_fake_and_real(build_agent, real):
    agent = build_agent(real)
    fake = FakeClient(CANNED, chunk_delay_s=0)
    agent.client_for = lambda model: fake if model == NO_MODEL else real
    return agent


def test_canned_prompts_show_only_while_no_model_is_selected(live_page):
    page = live_page(picker=picker_entries(CANNED))
    canned = page.get_by_role("button", name=FIRST)
    expect(canned).to_be_hidden()  # GLM 5.3 is selected

    choose(page, "No model")
    expect(canned).to_be_visible()
    expect(page.locator("body")).not_to_contain_text("Fake Mode")
    expect(page.locator("button[title='Effort for the next prompt']")).to_be_disabled()

    choose(page, "Claude Opus 5.5")
    expect(canned).to_be_hidden()


def test_a_canned_prompt_sends_a_fake_prompt_even_with_a_real_model_selected(build_agent, live_page):
    real = ScriptedLLM()
    page = live_page(agent=agent_with_fake_and_real(build_agent, real), picker=picker_entries(CANNED))
    choose(page, "No model")
    page.get_by_role("button", name=FIRST).click()  # visible only under No model
    expect(page.locator("#chat")).to_contain_text("No model")
    choose(page, "Claude Opus 5.5")
    expect(page.get_by_role("button", name=FIRST)).to_be_hidden()
    assert real.calls == []


def test_with_no_key_the_app_preselects_no_model_and_shows_canned_prompts(live_page):
    page = live_page(has_api_key=False, picker=picker_entries(CANNED))

    expect(page.locator(TRIGGER)).to_have_text("No model")
    expect(page.locator("#model")).to_have_value(NO_MODEL)
    expect(page.locator("#context-meter")).to_contain_text("no context used")
    expect(page.get_by_role("button", name=FIRST)).to_be_visible()


def test_a_refused_prompt_shows_its_message_in_the_page(live_page):
    page = live_page(has_api_key=False)
    page.evaluate("document.getElementById('model').removeAttribute(':value'); document.getElementById('model').value = 'anthropic/claude-opus-5.5'")

    page.evaluate("htmx.ajax('POST', '/chat', { target: '#chat', swap: 'beforeend', values: { message: 'hi', max_rows: '10', model: 'anthropic/claude-opus-5.5' } })")

    expect(page.get_by_role("alert")).to_contain_text("OPENROUTER_API_KEY")


def test_one_conversation_mixes_fake_and_real_turns_each_trace_showing_its_model(build_agent, live_page):
    page = live_page(agent=agent_with_fake_and_real(build_agent, ScriptedLLM([TextChunk("real answer")])))
    box = page.locator("textarea[name=message]")
    meter = page.locator("#context-meter")
    choose(page, "No model")
    box.fill("anything")
    box.press("Enter")
    expect(page.locator("#chat")).to_contain_text("no language model is involved")
    expect(meter).to_contain_text("no context used")

    choose(page, "Claude Opus 5.5")
    box.fill("now for real")
    box.press("Enter")
    expect(page.locator("#chat")).to_contain_text("real answer")
    expect(meter).to_contain_text("100 of 1M")

    traces = page.locator(".trace-totals")
    expect(traces).to_have_count(2)
    expect(traces.nth(0)).to_contain_text("No model")
    expect(traces.nth(1)).to_contain_text("anthropic/claude-opus-5.5")
    assert page.locator("input#conversation_id").input_value() != ""

    choose(page, "No model")  # the meter ignores the fake turn and shows no model's usage
    expect(meter).to_contain_text("no context used")
    choose(page, "GLM 5.3")
    expect(meter).to_contain_text("100 of 1.05M")  # the one real call still counts


def test_trace_shows_cached_tokens_only_when_some_were_cached(build_agent, live_page):
    cached = ScriptedLLM([TextChunk("first")])
    cached.cached_tokens = 80
    page = live_page(agent=agent_with_fake_and_real(build_agent, cached))
    box = page.locator("textarea[name=message]")
    choose(page, "Claude Opus 5.5")
    box.fill("hello")
    box.press("Enter")
    expect(page.locator(".trace-totals")).to_contain_text("80 cached")
    page.locator("details.trace > summary").click()
    expect(page.locator(".trace-llm_step h4")).to_contain_text("80 cached")

    plain = live_page(agent=agent_with_fake_and_real(build_agent, ScriptedLLM([TextChunk("second")])))
    plain.locator("textarea[name=message]").fill("hello")
    choose(plain, "Claude Opus 5.5")
    plain.locator("textarea[name=message]").press("Enter")
    expect(plain.locator(".trace-totals")).to_contain_text("anthropic/claude-opus-5.5")
    expect(plain.locator(".trace-totals")).not_to_contain_text("cached")


def test_trace_shows_cache_write_tokens_only_when_some_were_written(build_agent, live_page):
    writing = ScriptedLLM([TextChunk("first")])
    writing.cache_write_tokens = 70
    page = live_page(agent=agent_with_fake_and_real(build_agent, writing))
    box = page.locator("textarea[name=message]")
    choose(page, "Claude Opus 5.5")
    box.fill("hello")
    box.press("Enter")
    expect(page.locator(".trace-totals")).to_contain_text("70 cache write")
    page.locator("details.trace > summary").click()
    expect(page.locator(".trace-llm_step h4")).to_contain_text("70 cache write")

    plain = live_page(agent=agent_with_fake_and_real(build_agent, ScriptedLLM([TextChunk("second")])))
    plain.locator("textarea[name=message]").fill("hello")
    choose(plain, "Claude Opus 5.5")
    plain.locator("textarea[name=message]").press("Enter")
    expect(plain.locator(".trace-totals")).to_contain_text("anthropic/claude-opus-5.5")
    expect(plain.locator(".trace-totals")).not_to_contain_text("cache write")
