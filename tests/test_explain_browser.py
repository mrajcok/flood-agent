"""Each "Generated SQL" in a Trace has an Explain button that shows its Query Plan in a dialog, without the LLM."""

from conftest import ScriptedLLM, tool_call
from playwright.sync_api import expect
from test_notebook_browser import run_in_new_notebook

from flood_agent.llm import TextChunk


def trace_with(live_page, build_agent, sql):
    llm = ScriptedLLM([tool_call("run_query", sql=sql, title="t")], [TextChunk("Done.")])
    page = live_page(agent=build_agent(llm))
    run_in_new_notebook(page, "go")
    expect(page.locator(".reply", has_text="Done.")).to_be_visible()
    page.locator("details.trace > summary").click()
    return page, llm


def test_explain_shows_the_query_plan_in_a_dialog(live_page, build_agent):
    sql = "SELECT dport FROM flows WHERE etime > 5"
    page, llm = trace_with(live_page, build_agent, sql)
    calls = len(llm.calls)
    label = page.locator("details.trace .sql-label", has_text="Generated SQL")
    label.get_by_role("button", name="Explain").click()
    dialog = page.locator("dialog.explain")
    expect(dialog).to_be_visible()
    expect(dialog.locator("pre.sql")).to_have_text(sql)
    expect(dialog).to_contain_text("Plan as of now")
    expect(dialog.locator(".plan")).to_contain_text("SEARCH flows USING INDEX flows_etime")
    assert len(llm.calls) == calls  # no LLM
    dialog.get_by_role("button", name="Close").click()
    expect(dialog).to_be_hidden()


def test_rejected_sql_explains_why(live_page, build_agent):
    page, _ = trace_with(live_page, build_agent, "DELETE FROM flows")
    page.locator("details.trace .sql-label").get_by_role("button", name="Explain").click()
    dialog = page.locator("dialog.explain")
    expect(dialog.locator(".error")).to_contain_text("Only a single read-only SELECT")
    expect(dialog.locator(".plan")).to_be_hidden()


def test_a_subquery_step_is_indented_under_its_parent(live_page, build_agent):
    page, _ = trace_with(live_page, build_agent, "SELECT COUNT(*) FROM flows WHERE dport IN (SELECT dport FROM flows LIMIT 1)")
    page.locator("details.trace .sql-label").get_by_role("button", name="Explain").click()
    steps = page.locator("dialog.explain .plan li")
    expect(steps).to_have_count(3)
    pad = "e => parseFloat(getComputedStyle(e).paddingLeft)"
    assert steps.nth(2).evaluate(pad) > steps.nth(1).evaluate(pad)
