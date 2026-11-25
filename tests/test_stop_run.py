"""Stopping a running AI Cell: what it had produced is kept, and its events still end with a Trace."""

import asyncio

import pytest
from conftest import ScriptedLLM, add_ai_cell, collect, latest_run, run_of, tables_in, text_of, tool_call

from flood_agent.catalog import CellRun
from flood_agent.events import LlmCallStarted, TextDelta, TraceLive, TraceReady, TurnEnd
from flood_agent.llm import TextChunk
from flood_agent.mcp_connection import McpConnection
from flood_agent.notebook import UnknownCell

SQL = "SELECT dport, SUM(bytes) AS total_bytes FROM flows GROUP BY dport ORDER BY total_bytes DESC"


class HangingLLM:
    """Plays the scripted steps, then streams `text` and waits for ever, like a slow model."""

    def __init__(self, *steps, text="Partial answer"):
        self.scripted = ScriptedLLM(*steps)
        self.text = text

    async def stream_chat(self, messages, tools, reasoning=None, session_id=None):
        if self.scripted.steps:
            async for chunk in self.scripted.stream_chat(messages, tools, reasoning, session_id):
                yield chunk
            return
        yield TextChunk(self.text)
        await asyncio.Event().wait()


async def stopped_run(agent, notebook, prompt="q"):
    """Run a cell until it has streamed some text, stop it, and return all its events."""
    cell = add_ai_cell(agent, notebook, prompt=prompt)
    events = []
    async for ev in agent.notebooks.run_cell(notebook, cell):
        events.append(ev)
        if isinstance(ev, TextDelta) and "Partial" in ev.text:
            assert agent.notebooks.stop_cell(notebook, cell)
    return events


async def test_stopping_mid_stream_keeps_the_text_and_ends_with_a_stopped_trace(make_agent):
    agent = await make_agent(HangingLLM())
    nb = agent.notebooks.create_notebook()
    events = await stopped_run(agent, nb)

    assert isinstance(events[-1], TurnEnd)
    trace = next(e.trace for e in events if isinstance(e, TraceReady))
    assert [e["kind"] for e in trace["entries"]] == ["llm_step", "stopped"]
    assert trace["entries"][0]["raw_response"]["content"] == "Partial answer"
    assert "Stopped by user" in text_of(events)
    run = run_of(agent, nb)
    assert run["status"] == "stopped"
    assert run["messages"][-1] == {"role": "assistant", "content": "Partial answer\n\n*Stopped by user.*"}


async def test_stopping_after_earlier_calls_keeps_them_and_a_valid_history(make_agent):
    llm = HangingLLM([TextChunk("Looking."), tool_call("run_query", sql=SQL, title="t")])
    agent = await make_agent(llm)
    nb = agent.notebooks.create_notebook()
    events = await stopped_run(agent, nb)

    messages = run_of(agent, nb)["messages"]
    assert [m["role"] for m in messages] == ["user", "assistant", "tool", "assistant"]  # the finished call, its result, then the stop note
    assert messages[1]["tool_calls"]
    trace = next(e.trace for e in events if isinstance(e, TraceReady))
    assert trace["totals"]["llm_steps"] == 2


async def test_stopping_an_unknown_or_finished_cell_does_nothing(make_agent):
    agent = await make_agent(ScriptedLLM([TextChunk("hi")]))
    nb = agent.notebooks.create_notebook()
    with pytest.raises(UnknownCell):
        agent.notebooks.stop_cell(nb, "nope")
    cell = add_ai_cell(agent, nb, prompt="q")
    async for _ in agent.notebooks.run_cell(nb, cell):
        pass
    assert not agent.notebooks.stop_cell(nb, cell)


async def test_a_run_announces_each_llm_call_and_each_trace_entry_as_it_happens(make_agent):
    llm = ScriptedLLM([tool_call("run_query", sql=SQL, title="t")], [TextChunk("done")])
    agent = await make_agent(llm)
    events = await collect(agent, "q")
    assert [e.number for e in events if isinstance(e, LlmCallStarted)] == [1, 2]
    live = [e.entry["kind"] for e in events if isinstance(e, TraceLive)]
    assert live[:2] == ["llm_step", "tool_call"] and live.count("llm_step") == 2
    order = [type(e).__name__ for e in events if isinstance(e, (LlmCallStarted, TraceLive))]
    assert order[0] == "LlmCallStarted"


async def test_a_stopped_run_keeps_the_table_its_query_registered_with_that_run(make_agent):
    agent = await make_agent(HangingLLM([tool_call("run_query", sql=SQL, title="Ports")]))
    nb = agent.notebooks.create_notebook()
    await stopped_run(agent, nb, "ports please")

    (table,) = tables_in(agent, nb)
    cell = agent.notebooks.notebook_page(nb).cells[0]
    assert table.owner == CellRun(cell.cell.id, cell.run["id"])
    assert (table.title, table.request) == ("Ports", "ports please")


async def test_a_run_stopped_the_instant_its_tool_call_returns_still_registers_the_table(make_agent, monkeypatch):
    agent = await make_agent(HangingLLM([tool_call("run_query", sql=SQL, title="Ports")]))
    real_call_tool = McpConnection.call_tool
    nb = agent.notebooks.create_notebook()
    cell = add_ai_cell(agent, nb, prompt="ports please")

    async def stop_after_the_call(self, *args, **kwargs):
        result = await real_call_tool(self, *args, **kwargs)
        assert agent.notebooks.stop_cell(nb, cell)  # cancellation lands at the next await after the tool call returns
        return result

    monkeypatch.setattr(McpConnection, "call_tool", stop_after_the_call)
    async for _ in agent.notebooks.run_cell(nb, cell):
        pass

    (table,) = tables_in(agent, nb)
    assert table.title == "Ports"


async def test_a_registration_failure_is_not_reported_as_the_tool_failing(make_agent):
    agent = await make_agent(HangingLLM([tool_call("run_query", sql=SQL, title="Ports")]))

    def broken(*args, **kwargs):
        raise RuntimeError("catalog down")

    agent.tables.register_for = broken
    nb = agent.notebooks.create_notebook()
    await stopped_run(agent, nb, "ports please")

    tool_messages = [m["content"] for m in run_of(agent, nb)["messages"] if m["role"] == "tool"]
    assert tool_messages and "could not be recorded" in tool_messages[0]
    assert "failed to run" not in tool_messages[0]


async def abandoned_run(agent, notebook, cell, until):
    """Run a cell until `until(event)` holds, then leave, as a closed page does: the event stream is abandoned."""
    events = agent.notebooks.run_cell(notebook, cell)
    async for ev in events:
        if until(ev):
            break
    await events.aclose()


async def test_closing_the_page_mid_text_stops_the_run_and_keeps_its_partial_answer(make_agent):
    agent = await make_agent(HangingLLM())
    nb = agent.notebooks.create_notebook()
    cell = add_ai_cell(agent, nb, prompt="q")
    await abandoned_run(agent, nb, cell, lambda ev: isinstance(ev, TextDelta) and "Partial" in ev.text)

    run = run_of(agent, nb)
    assert run["status"] == "stopped" and run["error"] is None
    assert run["messages"][-1] == {"role": "assistant", "content": "Partial answer\n\n*Stopped by user.*"}
    assert [e["kind"] for e in run["trace"]["entries"]] == ["llm_step", "stopped"]
    assert run["trace"]["entries"][0]["raw_response"]["content"] == "Partial answer"


async def test_a_run_whose_page_was_closed_reaches_the_cell_below_as_stopped_by_the_user(make_agent):
    agent = await make_agent(HangingLLM())
    nb = agent.notebooks.create_notebook()
    a, b = add_ai_cell(agent, nb, prompt="ask A"), add_ai_cell(agent, nb, prompt="ask B")
    await abandoned_run(agent, nb, a, lambda ev: isinstance(ev, TextDelta) and "Partial" in ev.text)
    scripted = ScriptedLLM([TextChunk("ok")])
    agent.client_for = lambda model: scripted
    async for _ in agent.notebooks.run_cell(nb, b):
        pass

    assert [m["content"] for m in scripted.calls[0][1:]] == [
        "ask A", "Partial answer\n\n(stopped by the user)", "ask B"]


async def test_closing_the_page_before_any_text_stops_the_run_with_no_answer_text(make_agent):
    agent = await make_agent(HangingLLM(text=""))
    nb = agent.notebooks.create_notebook()
    cell = add_ai_cell(agent, nb, prompt="q")
    await abandoned_run(agent, nb, cell, lambda ev: isinstance(ev, LlmCallStarted))

    run = run_of(agent, nb)
    assert run["status"] == "stopped"
    assert run["messages"] == [{"role": "user", "content": "q"}, {"role": "assistant", "content": "*Stopped by user.*"}]


async def test_closing_the_app_before_a_run_takes_its_first_step_stops_it(make_agent):
    agent = await make_agent(HangingLLM())
    nb = agent.notebooks.create_notebook()
    cell = add_ai_cell(agent, nb, prompt="q")
    events = agent.notebooks.run_cell(nb, cell)

    async def first_event():
        return await anext(events)

    first = asyncio.create_task(first_event())
    await asyncio.sleep(0)  # the stream has started the Run; the Run's own task is scheduled but has not run yet
    await agent.aclose()  # cancels the Run before it has done anything

    assert isinstance(await asyncio.wait_for(first, 5), TurnEnd)  # the stream ends; it is not left waiting
    await events.aclose()
    run = run_of(agent, nb)
    assert run["status"] == "stopped" and run["error"] is None


async def test_an_ai_cell_cannot_run_once_its_runs_are_closed(make_agent):
    agent = await make_agent(ScriptedLLM([TextChunk("hi")]))
    nb = agent.notebooks.create_notebook()
    cell = add_ai_cell(agent, nb, prompt="q")
    await agent.ai_cells.aclose()

    with pytest.raises(RuntimeError, match="closed"):
        [e async for e in agent.ai_cells.run(agent.notebooks.cell(nb, cell))]
    assert latest_run(agent, nb, cell) is None
