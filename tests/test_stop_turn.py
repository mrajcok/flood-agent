"""Stopping a running turn: what it had produced is kept, and its events still end with a Trace."""

import asyncio

from conftest import ScriptedLLM, text_of, tool_call

from flood_agent.agent import Conversation, LlmCallStarted, TextDelta, TraceLive, TraceReady, TurnEnd
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


async def stopped_turn(agent, conversation):
    """Run a turn until it has streamed some text, stop it, and return all its events."""
    events = []
    async for ev in agent.run_turn(conversation, "q", 1000, turn_key="t1"):
        events.append(ev)
        if isinstance(ev, TextDelta) and "Partial" in ev.text:
            assert agent.stop_turn("t1")
    return events


async def test_stopping_mid_stream_keeps_the_text_and_ends_with_a_stopped_trace(make_agent):
    agent = await make_agent(HangingLLM())
    conversation = Conversation()
    events = await stopped_turn(agent, conversation)

    assert isinstance(events[-1], TurnEnd)
    trace = next(e.trace for e in events if isinstance(e, TraceReady))
    assert [e["kind"] for e in trace["entries"]] == ["llm_step", "stopped"]
    assert trace["entries"][0]["raw_response"]["content"] == "Partial answer"
    assert "Stopped by user" in text_of(events)
    assert conversation.messages[-1] == {"role": "assistant", "content": "Partial answer\n\n*Stopped by user.*"}
    assert agent.store.messages(conversation.id)[-1]["content"] == conversation.messages[-1]["content"]


async def test_stopping_after_earlier_calls_keeps_them_and_a_valid_history(make_agent):
    llm = HangingLLM([TextChunk("Looking."), tool_call("run_query", sql=SQL, title="t")])
    agent = await make_agent(llm)
    conversation = Conversation()
    events = await stopped_turn(agent, conversation)

    roles = [m["role"] for m in conversation.messages]
    assert roles == ["user", "assistant", "tool", "assistant"]  # the finished call, its result, then the stop note
    assert conversation.messages[1]["tool_calls"]
    trace = next(e.trace for e in events if isinstance(e, TraceReady))
    assert trace["totals"]["llm_steps"] == 2


async def test_the_next_prompt_sees_the_stopped_text(make_agent):
    llm = HangingLLM()
    agent = await make_agent(llm)
    conversation = Conversation()
    await stopped_turn(agent, conversation)
    llm.scripted = ScriptedLLM([TextChunk("continuing")])
    async for _ in agent.run_turn(conversation, "continue", 1000):
        pass
    seen = [m["content"] for m in llm.scripted.calls[0] if m["role"] == "assistant"]
    assert seen == ["Partial answer\n\n*Stopped by user.*"]


async def test_stopping_an_unknown_or_finished_turn_does_nothing(make_agent):
    agent = await make_agent(ScriptedLLM([TextChunk("hi")]))
    assert not agent.stop_turn("nope")
    async for _ in agent.run_turn(Conversation(), "q", 1000, turn_key="done"):
        pass
    assert not agent.stop_turn("done")


async def test_a_turn_announces_each_llm_call_and_each_trace_entry_as_it_happens(make_agent):
    llm = ScriptedLLM([tool_call("run_query", sql=SQL, title="t")], [TextChunk("done")])
    agent = await make_agent(llm)
    events = [e async for e in agent.run_turn(Conversation(), "q", 1000)]
    assert [e.number for e in events if isinstance(e, LlmCallStarted)] == [1, 2]
    live = [e.entry["kind"] for e in events if isinstance(e, TraceLive)]
    assert live[:2] == ["llm_step", "tool_call"] and live.count("llm_step") == 2
    order = [type(e).__name__ for e in events if isinstance(e, (LlmCallStarted, TraceLive))]
    assert order[0] == "LlmCallStarted"
