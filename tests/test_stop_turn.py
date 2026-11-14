"""Stopping a running AI Cell: what it had produced is kept, and its events still end with a Trace."""

import asyncio

from conftest import ScriptedLLM, collect, run_of, tables_in, text_of, tool_call

from flood_agent.agent import LlmCallStarted, TextDelta, TraceLive, TraceReady, TurnEnd
from flood_agent.catalog import CellRun
from flood_agent.llm import TextChunk

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


async def stopped_turn(agent, notebook, prompt="q"):
    """Run a cell until it has streamed some text, stop it, and return all its events."""
    cell = agent.add_ai_cell(notebook, prompt=prompt)
    events = []
    async for ev in agent.run_cell(notebook, cell):
        events.append(ev)
        if isinstance(ev, TextDelta) and "Partial" in ev.text:
            assert agent.stop_cell(cell)
    return events


async def test_stopping_mid_stream_keeps_the_text_and_ends_with_a_stopped_trace(make_agent):
    agent = await make_agent(HangingLLM())
    nb = agent.create_notebook()
    events = await stopped_turn(agent, nb)

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
    nb = agent.create_notebook()
    events = await stopped_turn(agent, nb)

    messages = run_of(agent, nb)["messages"]
    assert [m["role"] for m in messages] == ["user", "assistant", "tool", "assistant"]  # the finished call, its result, then the stop note
    assert messages[1]["tool_calls"]
    trace = next(e.trace for e in events if isinstance(e, TraceReady))
    assert trace["totals"]["llm_steps"] == 2


async def test_stopping_an_unknown_or_finished_cell_does_nothing(make_agent):
    agent = await make_agent(ScriptedLLM([TextChunk("hi")]))
    assert not agent.stop_cell("nope")
    nb = agent.create_notebook()
    cell = agent.add_ai_cell(nb, prompt="q")
    async for _ in agent.run_cell(nb, cell):
        pass
    assert not agent.stop_cell(cell)


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
    nb = agent.create_notebook()
    await stopped_turn(agent, nb, "ports please")

    (table,) = tables_in(agent, nb)
    cell = agent.notebooks.cells(nb)[0]
    assert table.owner == CellRun(cell["id"], cell["run"]["id"])
    assert (table.title, table.request) == ("Ports", "ports please")


async def test_a_run_stopped_the_instant_its_tool_call_returns_still_registers_the_table(make_agent):
    agent = await make_agent(HangingLLM([tool_call("run_query", sql=SQL, title="Ports")]))
    connection = agent._connection
    real_call_tool = connection.call_tool
    nb = agent.create_notebook()
    cell = agent.add_ai_cell(nb, prompt="ports please")

    async def stop_after_the_call(*args, **kwargs):
        result = await real_call_tool(*args, **kwargs)
        assert agent.stop_cell(cell)  # cancellation lands at the next await after the tool call returns
        return result

    connection.call_tool = stop_after_the_call
    async for _ in agent.run_cell(nb, cell):
        pass

    (table,) = tables_in(agent, nb)
    assert table.title == "Ports"


async def test_a_registration_failure_is_not_reported_as_the_tool_failing(make_agent):
    agent = await make_agent(HangingLLM([tool_call("run_query", sql=SQL, title="Ports")]))

    def broken(*args, **kwargs):
        raise RuntimeError("catalog down")

    agent.tables.register_for = broken
    nb = agent.create_notebook()
    await stopped_turn(agent, nb, "ports please")

    tool_messages = [m["content"] for m in run_of(agent, nb)["messages"] if m["role"] == "tool"]
    assert tool_messages and "could not be recorded" in tool_messages[0]
    assert "failed to run" not in tool_messages[0]
