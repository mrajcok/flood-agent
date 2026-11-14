"""The LLM's reply text is shown as rendered, sanitised Markdown, live and after reloading the notebook."""

from conftest import ScriptedLLM, last_tool_result, tool_call
from playwright.sync_api import expect
from test_notebook_browser import run_in_new_notebook

from flood_agent.llm import TextChunk

SQL = "SELECT dport, SUM(bytes) AS total_bytes FROM flows GROUP BY dport ORDER BY total_bytes DESC"


def script():
    return [
        # Markdown split across stream chunks, then a table, then more Markdown.
        [TextChunk("Two **fix"), TextChunk("es**:\n\n1. Recast as `REAL`\n2. Simplified"),
         TextChunk(" the spec\n\n<img src=x onerror=\"window.pwned=1\"><script>window.pwned=1</script>"),
         tool_call("run_query", sql=SQL, title="t")],
        lambda msgs: [tool_call("show_table", result_table=last_tool_result(msgs)["result_table"])],
        [TextChunk("A *closing* note.")],
    ]


def check_rendered(turn):
    reply = turn.locator(".reply .md")  # the rendered text, not the Display between
    expect(reply.locator("strong")).to_have_text(["fixes"])
    expect(reply.locator("ol > li")).to_have_text(["Recast as REAL", "Simplified the spec"])
    expect(reply.locator("code")).to_have_text(["REAL"])
    expect(reply.locator("em")).to_have_text(["closing"])
    for segment in reply.all():
        expect(segment).not_to_contain_text("**")
    expect(turn.locator(".reply img[onerror], .reply script")).to_have_count(0)
    assert turn.page.evaluate("() => window.pwned") is None
    # The table stays between the two pieces of text.
    order = turn.evaluate("""t => {
        const ol = t.querySelector('.reply .md ol'), display = t.querySelector('.display'), em = t.querySelector('.reply .md em');
        return [ol.compareDocumentPosition(display) & Node.DOCUMENT_POSITION_FOLLOWING,
                display.compareDocumentPosition(em) & Node.DOCUMENT_POSITION_FOLLOWING].every(Boolean);
    }""")
    assert order


def test_reply_markdown_is_rendered_and_sanitised_live_and_from_history(live_page, build_agent):
    page = live_page(agent=build_agent(ScriptedLLM(*script())))
    cell = run_in_new_notebook(page, "what fixed it?")
    expect(cell.locator(".cell-foot")).to_be_visible()  # the run is done
    check_rendered(cell)

    page.reload()
    check_rendered(page.locator(".cellblock").first)
