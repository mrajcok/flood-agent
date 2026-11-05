"""A turn's text, tables and charts appear in the order the LLM produced them, live and after a reload."""

from conftest import ScriptedLLM, last_tool_result, tool_call
from playwright.sync_api import expect

from flood_agent.llm import TextChunk

SQL = "SELECT dport, SUM(bytes) AS total_bytes FROM flows GROUP BY dport ORDER BY total_bytes DESC"


def reply_script():
    return [
        [TextChunk("Here they are:"), tool_call("run_query", sql=SQL, title="t")],
        lambda msgs: [tool_call("show_table", result_table=last_tool_result(msgs)["result_table"])],
        [TextChunk("That is the table.")],
    ]


def order_in(turn):
    """The turn's text pieces and Displays, top to bottom, as 'text: ...' or 'display'."""
    return turn.evaluate("""t => {
        const out = [];
        const walk = n => {
            if (n.nodeType === Node.TEXT_NODE) { if (n.textContent.trim()) out.push('text: ' + n.textContent.trim()); return; }
            if (n.matches && n.matches('summary, .trace, details.trace, .thinking')) return;
            if (n.classList && n.classList.contains('display')) { out.push('display'); return; }
            n.childNodes.forEach(walk);
        };
        walk(t);
        /* Adjacent text pieces read as one: join them. */
        return out.reduce((a, x) => (x.startsWith('text: ') && a.length && a[a.length - 1].startsWith('text: ')
            ? (a[a.length - 1] += ' ' + x.slice(6), a) : (a.push(x), a)), []);
    }""")


def test_text_before_a_table_shows_above_it_and_text_after_below_it(live_page, build_agent):
    page = live_page(agent=build_agent(ScriptedLLM(*reply_script())))
    page.get_by_placeholder("Write a prompt").fill("show me ports")
    page.get_by_role("button", name="Send message (Enter)").click()
    turn = page.locator("details.turn").last
    expect(turn.locator("details.trace")).to_have_count(1)  # the Trace comes last: the turn is done
    expect(turn.locator(".display table")).to_be_visible()
    expected = ["text: Here they are:", "display", "text: That is the table."]
    assert order_in(turn) == expected

    page.locator("aside ul li a").first.click()  # the saved Conversation, rendered from history
    turn = page.locator("details.turn").last
    expect(turn.locator(".display table")).to_be_visible()
    assert order_in(turn) == expected
