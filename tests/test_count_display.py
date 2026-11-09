"""Every successful run_count_query makes a Count Display: a one-line item (title, number, "as of") saved
with its Conversation, shown in reply order, with no Retention (ADR 0006)."""

import re
import sqlite3
from contextlib import closing

from conftest import Clock, ScriptedLLM, collect, last_tool_result, tool_call

from flood_agent.agent import CountEvent, TextDelta
from flood_agent.llm import TextChunk

COUNT = "SELECT COUNT(*) FROM flows"


def counted(title="All flows", sql=COUNT):
    return ScriptedLLM(
        [TextChunk("Counting."), tool_call("run_count_query", sql=sql, title=title)], [TextChunk("There are 3 flows.")]
    )


async def test_a_count_query_emits_a_count_display_in_reply_order(make_agent):
    clock = Clock(1_700_000_000_000)
    llm = counted()
    agent = await make_agent(llm, clock=clock)

    events = await collect(agent, "how many flows?")

    shown = [e for e in events if isinstance(e, CountEvent)]
    assert [(e.title, e.value, e.sql, e.computed_at_ms) for e in shown] == [("All flows", 3, COUNT, 1_700_000_000_000)]
    order = ["text" if isinstance(e, TextDelta) else "count" for e in events if isinstance(e, (TextDelta, CountEvent))]
    assert order == ["text", "count", "text"]
    # The LLM still gets the number to state, and nothing about the Count Display.
    assert last_tool_result(llm.calls[1]) == {"success": True, "count": 3}


async def test_failed_and_rejected_counts_make_no_count_display(make_agent):
    steps = [[tool_call("run_count_query", call_id="a", sql="SELECT sip FROM flows", title="Bad")],
             [tool_call("run_count_query", call_id="b", sql="SELECT COUNT(*) FROM nope", title="Missing")],
             [TextChunk("sorry")]]
    agent = await make_agent(ScriptedLLM(*steps))

    events = await collect(agent, "how many?")

    assert not [e for e in events if isinstance(e, CountEvent)]


def ask(build_agent, web_client, llm=None, **overrides):
    clock = Clock(1_700_000_000_000)
    agent = build_agent(llm or counted(), clock=clock, **overrides)
    client = web_client(agent)
    posted = client.post("/chat", data={"message": "go", "conversation_id": "", "max_rows": "1000"})
    stream = client.get(posted.text.split('sse-connect="')[1].split('"')[0]).text
    cid = re.search(r'/c/([0-9a-f]+)', client.get("/").text).group(1)
    return client, agent, clock, cid, stream


def test_the_live_reply_and_the_reopened_conversation_both_show_the_count_display(build_agent, web_client):
    client, _, _, cid, stream = ask(build_agent, web_client)
    assert 'class="count-display"' in stream

    page = client.get(f"/c/{cid}").text
    assert page.count('class="count-display"') == 1
    assert "All flows" in page and 'data-as-of-ms="1700000000000"' in page
    assert page.rindex("Counting.") < page.index('class="count-display"') < page.rindex("There are 3 flows.")


def test_a_count_display_never_expires(build_agent, web_client):
    client, _, clock, cid, _ = ask(build_agent, web_client)
    clock.ms += 10 * 365 * 24 * 3_600_000
    assert 'class="count-display"' in client.get(f"/c/{cid}").text


def test_deleting_the_conversation_deletes_its_count_displays(build_agent, web_client):
    client, agent, _, cid, _ = ask(build_agent, web_client)
    client.post(f"/c/{cid}/delete", data={"tables": "delete_rt"}, follow_redirects=False)
    with closing(sqlite3.connect(agent.config.app_db)) as con:
        assert con.execute("SELECT COUNT(*) FROM displays WHERE kind='count'").fetchone() == (0,)
