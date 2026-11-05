"""The LLM's reply text is shown as rendered, sanitised Markdown, live and after reopening the Conversation."""

from conftest import ScriptedLLM, last_tool_result, tool_call
from playwright.sync_api import expect

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
    page.get_by_placeholder("Write a prompt").fill("what fixed it?")
    page.get_by_role("button", name="Send message (Enter)").click()
    turn = page.locator("details.turn").last
    expect(turn.locator("details.trace")).to_have_count(1)  # the turn is done
    check_rendered(turn)

    page.locator("aside ul li a").first.click()
    check_rendered(page.locator("details.turn").last)
