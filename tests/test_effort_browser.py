"""The strip's Effort dropdown: the selected model's levels, each model's own remembered level on a switch,
the per-model memory, and the disabled "no effort" state."""

from playwright.sync_api import expect


def model_button(page):
    return page.locator("button[title='Model for the next prompt']")


def effort_button(page):
    return page.locator("button[title='Effort for the next prompt']")


def pick_model(page, label):
    model_button(page).click()
    page.get_by_role("dialog", name="Model").get_by_role("button", name=label).click()


def pick_effort(page, level):
    effort_button(page).click()
    page.get_by_role("dialog", name="Reasoning effort").get_by_role("button", name=level, exact=True).click()


def test_the_dropdown_lists_the_selected_models_levels_with_its_default_selected(live_page):
    page = live_page()
    pick_model(page, "DeepSeek V4 Pro")

    expect(effort_button(page)).to_have_text("high")
    expect(page.locator("#effort")).to_have_value("high")
    effort_button(page).click()
    panel = page.get_by_role("dialog", name="Reasoning effort")
    expect(panel.get_by_role("heading")).to_have_text("Reasoning effort")
    expect(panel.locator(".item > span:nth-child(2)")).to_have_text(["max", "high", "low", "none"])


def test_glm_offers_max_high_and_low_with_max_selected(live_page):
    page = live_page()

    expect(model_button(page)).to_have_text("GLM 5.3")
    expect(effort_button(page)).to_have_text("max")
    effort_button(page).click()
    panel = page.get_by_role("dialog", name="Reasoning effort")
    expect(panel.locator(".item > span:nth-child(2)")).to_have_text(["max", "high", "low"])


def test_no_model_shows_a_disabled_no_effort_control_that_sends_nothing(live_page):
    page = live_page()
    pick_model(page, "No model")

    expect(effort_button(page)).to_have_text("no effort")
    expect(effort_button(page)).to_be_disabled()
    expect(page.locator("#effort")).to_have_value("")


def test_each_model_returns_to_its_own_remembered_effort_never_carrying_one_over(live_page):
    page = live_page()
    pick_model(page, "Claude Sonnet 5.5")
    pick_effort(page, "low")

    pick_model(page, "DeepSeek V4 Pro")  # offers low too, but nothing was chosen for it: its own default
    expect(effort_button(page)).to_have_text("high")
    expect(page.locator("#effort")).to_have_value("high")

    pick_model(page, "Claude Opus 5.5")  # never chosen for Opus: its default, not Sonnet's low
    expect(effort_button(page)).to_have_text("medium")
    pick_effort(page, "xhigh")

    pick_model(page, "Claude Sonnet 5.5")  # back to its own choice
    expect(effort_button(page)).to_have_text("low")
    pick_model(page, "Claude Opus 5.5")
    expect(effort_button(page)).to_have_text("xhigh")


def test_switching_away_and_back_restores_the_choice_made_for_that_model(live_page):
    page = live_page()
    pick_model(page, "Claude Sonnet 5.5")
    pick_effort(page, "max")

    pick_model(page, "GLM 5.3")
    expect(effort_button(page)).to_have_text("max")  # GLM's own default, not Sonnet's choice
    pick_effort(page, "low")
    pick_model(page, "Claude Sonnet 5.5")

    expect(effort_button(page)).to_have_text("max")
    page.reload()
    expect(effort_button(page)).to_have_text("max")  # remembered in the browser


def test_the_chosen_effort_travels_with_the_prompt(live_page):
    page = live_page()
    pick_model(page, "Claude Sonnet 5.5")
    pick_effort(page, "low")
    seen = []
    page.on("request", lambda r: seen.append(r.post_data) if r.url.endswith("/chat") else None)

    page.locator("textarea[name=message]").fill("hello")
    page.locator("textarea[name=message]").press("Enter")

    expect(page.locator("#chat")).to_contain_text("hello")
    assert seen and "effort=low" in seen[0]
