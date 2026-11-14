"""Every successful run_count_query makes a Count Display: a one-line item (title, number, "as of") saved
with its AI Cell's Run, shown in reply order, with no Retention (ADR 0006)."""

import sqlite3
from contextlib import closing

from conftest import Clock, ScriptedLLM, collect, last_tool_result, run_of, tool_call
from conftest import ask as run_in_notebook

from flood_agent.agent import CountEvent, DisplayEvent, TextDelta
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
    nb, _, stream = run_in_notebook(client, "go")
    return client, agent, clock, nb, stream


def test_the_live_reply_and_the_reopened_notebook_both_show_the_count_display(build_agent, web_client):
    client, _, _, nb, stream = ask(build_agent, web_client)
    assert 'class="count-display"' in stream

    page = client.get(f"/n/{nb}").text
    assert page.count('class="count-display"') == 1
    assert "All flows" in page and 'data-as-of-ms="1700000000000"' in page
    assert page.index("Counting.") < page.index('class="count-display"') < page.rindex("There are 3 flows.")


def test_a_count_display_never_expires(build_agent, web_client):
    client, _, clock, nb, _ = ask(build_agent, web_client)
    clock.ms += 10 * 365 * 24 * 3_600_000
    assert 'class="count-display"' in client.get(f"/n/{nb}").text


def test_deleting_the_notebook_deletes_its_count_displays(build_agent, web_client):
    client, agent, _, nb, _ = ask(build_agent, web_client)
    client.post(f"/n/{nb}/delete", data={"tables": "delete_rt"}, follow_redirects=False)
    with closing(sqlite3.connect(agent.notebooks.path)) as con:
        assert con.execute("SELECT COUNT(*) FROM run_displays WHERE kind='count'").fetchone() == (0,)


async def test_a_mixed_step_shows_its_displays_in_call_order_live_and_after_a_reload(make_agent):
    llm = ScriptedLLM(
        [tool_call("run_query", call_id="q", sql="SELECT sip FROM flows", title="Sources")],
        lambda msgs: [tool_call("run_count_query", call_id="a", sql=COUNT, title="All flows"),
                      tool_call("show_table", call_id="b", result_table=last_tool_result(msgs)["result_table"]),
                      tool_call("run_count_query", call_id="c", sql=COUNT, title="Again")],
        [TextChunk("done")],
    )
    agent = await make_agent(llm)

    events = await collect(agent, "show me")

    live = ["count" if isinstance(e, CountEvent) else e.kind for e in events if isinstance(e, (CountEvent, DisplayEvent))]
    (nb,) = [n["id"] for n in agent.notebooks.list_notebooks()]
    reloaded = [i["kind"] for i in agent.notebooks.transcript(run_of(agent, nb)["id"]) if i["type"] == "display"]
    assert reloaded == ["count", "table", "count"]
    assert live == reloaded
