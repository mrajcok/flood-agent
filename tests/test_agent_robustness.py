"""Agent turn robustness: bad LLM tool arguments, partial failures, atomic history, MCP
subprocess teardown, Result Table ownership in show_*. Scripted LLM; tests marked `subprocess`
run a real MCP server process, the rest an in-process one."""

import asyncio
import json

import pytest
from conftest import ScriptedLLM, collect, last_tool_result, mcp_children, tool_call

import flood_defender.agent as agent_mod
from flood_defender.agent import Conversation, DisplayEvent, TextDelta, TraceReady, TurnEnd, TurnError
from flood_defender.llm import LLMError, TextChunk, ToolCallChunk

SQL = "SELECT dport, SUM(bytes) AS total_bytes FROM flows GROUP BY dport ORDER BY total_bytes DESC"


def text_of(events):
    return "".join(e.text for e in events if isinstance(e, TextDelta))


def tool_msgs(messages):
    return [json.loads(m["content"]) for m in messages if m["role"] == "tool"]


def assert_valid_history(messages):
    """Every assistant tool_calls message is followed by a tool message for each id."""
    for i, m in enumerate(messages):
        if m["role"] == "assistant" and m.get("tool_calls"):
            ids = {tc["id"] for tc in m["tool_calls"]}
            following = set()
            for n in messages[i + 1:]:
                if n["role"] != "tool":
                    break
                following.add(n["tool_call_id"])
            assert ids <= following, (i, ids, following)


# --- item 2: LLM-controlled arguments ---------------------------------------------------------


async def test_invalid_json_arguments_give_the_llm_an_error_not_a_crash(make_agent):
    llm = ScriptedLLM(
        [ToolCallChunk("c1", "run_query", "{not json")],
        [tool_call("run_query", "c2", sql=SQL, title="t")],
        [TextChunk("recovered")],
    )
    events = await collect(await make_agent(llm), "q")
    assert isinstance(events[-1], TurnEnd) and text_of(events) == "recovered"
    first = last_tool_result(llm.calls[1])
    assert first["success"] is False and "JSON" in first["error"]
    assert last_tool_result(llm.calls[2])["success"] is True


async def test_non_object_arguments_are_an_error_result(make_agent):
    llm = ScriptedLLM([ToolCallChunk("c1", "show_table", "[1, 2]")], [TextChunk("ok")])
    events = await collect(await make_agent(llm), "q")
    assert isinstance(events[-1], TurnEnd)
    assert last_tool_result(llm.calls[1])["success"] is False


async def test_run_query_without_sql_is_an_error_result_and_counts_as_a_failed_attempt(make_agent):
    llm = ScriptedLLM(
        [tool_call("run_query", "c1", title="t")],
        [tool_call("run_query", "c2", title="t")],
        [tool_call("run_query", "c3", title="t")],
    )
    events = await collect(await make_agent(llm), "q")
    r = last_tool_result(llm.calls[1])
    assert r["success"] is False and "sql" in r["error"]
    assert agent_mod.GIVE_UP_MESSAGE in text_of(events)
    assert isinstance(events[-1], TurnEnd)


# --- items 1, 7: failures partway through a turn ------------------------------------------------


async def test_failed_llm_step_keeps_history_valid_surfaces_error_and_saves_trace(make_agent):
    def boom(messages):
        raise LLMError("The model provider reported an error: upstream exploded")

    llm = ScriptedLLM([tool_call("run_query", "c1", sql=SQL, title="t")], boom)
    agent = await make_agent(llm)
    conv = Conversation()
    events = await collect(agent, "q", conv)

    assert "upstream exploded" in text_of(events)
    assert any(isinstance(e, TurnError) for e in events) and isinstance(events[-1], TurnEnd)
    (trace,) = [e.trace for e in events if isinstance(e, TraceReady)]
    errors = [e for e in trace["entries"] if e["kind"] == "error"]
    assert errors and "upstream exploded" in errors[0]["message"]

    saved = agent.store.messages(conv.id)
    assert_valid_history(saved)
    assert saved[-1]["role"] == "assistant" and "upstream exploded" in saved[-1]["content"]
    assert [m for m in saved if m["role"] == "assistant" and not m["content"] and not m.get("tool_calls")] == []
    assert any(t["type"] == "trace" for t in agent.store.transcript(conv.id))  # Trace persisted


async def test_unexpected_exception_is_reported_not_raised(make_agent):
    def boom(messages):
        raise RuntimeError("kaboom")

    agent = await make_agent(ScriptedLLM(boom))
    conv = Conversation()
    events = await collect(agent, "q", conv)
    (err,) = [e for e in events if isinstance(e, TurnError)]
    assert "RuntimeError" in err.detail and "kaboom" in err.detail
    assert isinstance(events[-1], TurnEnd)
    assert any(t["type"] == "trace" for t in agent.store.transcript(conv.id))


async def test_open_conversation_repairs_corrupt_saved_history(make_agent):
    agent = await make_agent(ScriptedLLM())
    cid = agent.store.create_conversation("c")
    turn = agent.store.start_turn(cid, "hi")
    call = lambda i: {"id": i, "type": "function", "function": {"name": "run_query", "arguments": "{}"}}  # noqa: E731
    for m in [
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": "", "tool_calls": [call("a"), call("b")]},
        {"role": "tool", "tool_call_id": "a", "content": "{}"},  # "b" never answered
        {"role": "user", "content": "again"},
        {"role": "assistant", "content": "", "tool_calls": [call("c")]},
        {"role": "tool", "tool_call_id": "c", "content": "{}"},
        {"role": "assistant", "content": "fine"},
        {"role": "assistant", "content": "", "tool_calls": [call("d")]},  # trailing, unanswered
    ]:
        agent.store.add_message(cid, turn, m)
    msgs = agent.open_conversation(cid).messages
    assert_valid_history(msgs)
    assert [m["role"] for m in msgs] == ["user", "user", "assistant", "tool", "assistant"]


async def test_steps_exhausted_tells_the_user_and_saves_the_trace(make_agent):
    llm = ScriptedLLM(
        [tool_call("run_query", "c1", sql=SQL, title="t")],
        [tool_call("run_query", "c2", sql=SQL, title="t")],
    )
    agent = await make_agent(llm, max_llm_steps=2)
    conv = Conversation()
    events = await collect(agent, "q", conv)
    assert agent_mod.STEP_LIMIT_MESSAGE in text_of(events)
    assert any(isinstance(e, TraceReady) for e in events) and isinstance(events[-1], TurnEnd)
    saved = agent.store.messages(conv.id)
    assert saved[-1] == {"role": "assistant", "content": agent_mod.STEP_LIMIT_MESSAGE}
    assert_valid_history(saved)


# --- item 3: no orphaned MCP subprocess ---------------------------------------------------------


@pytest.mark.subprocess
async def test_one_mcp_server_serves_several_turns_across_two_conversations(build_agent):
    def script():
        return ScriptedLLM([tool_call("run_query", "c1", sql=SQL, title="t")], [TextChunk("done")])

    agent = build_agent(script())
    assert mcp_children() == []  # constructing an Agent starts nothing
    await agent.start()
    (server,) = mcp_children()
    try:
        for _ in range(2):
            for conv in (Conversation(), Conversation()):
                agent.llm.steps = script().steps
                events = await collect(agent, "q", conv)
                assert isinstance(events[-1], TurnEnd)
                assert mcp_children() == [server]
    finally:
        await agent.aclose()
    assert mcp_children() == []


async def test_run_turn_on_an_agent_that_was_not_started_raises_a_clear_error(build_agent):
    agent = build_agent(ScriptedLLM([TextChunk("x")]))
    with pytest.raises(RuntimeError, match="not started"):
        await collect(agent, "q")


@pytest.mark.subprocess
async def test_agent_as_async_context_manager_starts_and_stops_the_server(build_agent):
    async with build_agent(ScriptedLLM([TextChunk("x")])) as agent:
        assert len(mcp_children()) == 1
        assert isinstance((await collect(agent, "q"))[-1], TurnEnd)
    assert mcp_children() == []


@pytest.mark.subprocess
async def test_no_mcp_subprocess_left_after_close_following_a_failed_turn(build_agent):
    def boom(messages):
        raise RuntimeError("x")

    agent = build_agent(ScriptedLLM(boom))
    await agent.start()
    await collect(agent, "q")
    await agent.aclose()
    assert mcp_children() == []


@pytest.mark.subprocess
async def test_consumer_stopping_early_leaves_the_server_running_and_the_next_turn_works(build_agent):
    llm = ScriptedLLM([TextChunk("partial"), tool_call("run_query", "c1", sql=SQL, title="t")], [TextChunk("x")])
    agent = build_agent(llm)
    await agent.start()
    try:
        (server,) = mcp_children()
        gen = agent.run_turn(Conversation(), "q")
        async for event in gen:
            if isinstance(event, TextDelta):
                break
        await gen.aclose()
        assert mcp_children() == [server]  # still running after the consumer left
        llm.steps = [[tool_call("run_query", "c2", sql=SQL, title="t")], [TextChunk("fine")]]
        events = await collect(agent, "again")
        assert isinstance(events[-1], TurnEnd) and text_of(events) == "fine"
        assert last_tool_result(llm.calls[-1])["success"] is True
    finally:
        await agent.aclose()
    assert mcp_children() == []


# --- item 4: show_table / show_chart ownership ---------------------------------------------------


# --- recovery: a dead MCP server is replaced -----------------------------------------------------


def kill_mcp_servers():
    import os
    import signal
    import time

    for pid in mcp_children():
        os.kill(pid, signal.SIGKILL)
    deadline = time.monotonic() + 5
    while mcp_children() and time.monotonic() < deadline:
        time.sleep(0.02)


async def kill_mcp_servers_and_settle(agent):
    """Kill the server and wait until the Agent's connection has noticed, as it would between real turns."""
    kill_mcp_servers()
    deadline = asyncio.get_running_loop().time() + 5
    while agent._connection.alive:
        assert asyncio.get_running_loop().time() < deadline, "the connection never noticed the kill"
        await asyncio.sleep(0.01)


@pytest.mark.subprocess
async def test_next_turn_succeeds_on_a_replacement_server_after_the_server_was_killed(build_agent):
    llm = ScriptedLLM([TextChunk("one")], [tool_call("run_query", "c1", sql=SQL, title="t")], [TextChunk("two")])
    agent = build_agent(llm)
    await agent.start()
    try:
        await collect(agent, "q1")
        (old,) = mcp_children()
        await kill_mcp_servers_and_settle(agent)
        events = await collect(agent, "q2")
        assert isinstance(events[-1], TurnEnd) and text_of(events) == "two"
        assert last_tool_result(llm.calls[-1])["success"] is True
        (new,) = mcp_children()
        assert new != old
    finally:
        await agent.aclose()
    assert mcp_children() == []


@pytest.mark.subprocess
async def test_server_dying_mid_turn_is_a_tool_error_and_the_turn_still_ends_cleanly(build_agent):
    def kill_then_call(messages):
        kill_mcp_servers()
        return [tool_call("run_query", "c1", sql=SQL, title="t")]

    llm = ScriptedLLM(kill_then_call, [TextChunk("sorry")])
    agent = build_agent(llm)
    await agent.start()
    try:
        conv = Conversation()
        events = await asyncio.wait_for(collect(agent, "q", conv), 30)
        assert any(isinstance(e, TraceReady) for e in events) and isinstance(events[-1], TurnEnd)
        trace = next(e for e in events if isinstance(e, TraceReady)).trace
        assert "tool_error" in json.dumps(trace, default=str)
        assert last_tool_result(llm.calls[-1])["success"] is False
        assert_valid_history(agent.store.messages(conv.id))
    finally:
        await agent.aclose()
    assert mcp_children() == []


@pytest.mark.subprocess
async def test_failed_restart_is_a_turn_error_with_an_error_trace_entry(build_agent, monkeypatch):
    import sys

    from mcp import StdioServerParameters

    agent = build_agent(ScriptedLLM([TextChunk("one")], [TextChunk("two")]))
    await agent.start()
    try:
        await collect(agent, "q1")
        await kill_mcp_servers_and_settle(agent)
        monkeypatch.setattr(
            agent, "_server_params", lambda: StdioServerParameters(command=sys.executable, args=["-c", "raise SystemExit(1)"])
        )
        events = await asyncio.wait_for(collect(agent, "q2"), 30)
        assert any(isinstance(e, TurnError) for e in events)
        assert any(isinstance(e, TraceReady) for e in events) and isinstance(events[-1], TurnEnd)
        trace = next(e for e in events if isinstance(e, TraceReady)).trace
        assert '"error"' in json.dumps(trace, default=str)
    finally:
        await agent.aclose()
    assert mcp_children() == []


async def first_conversation_table(agent, llm):
    conv = Conversation()
    llm.steps = [[tool_call("run_query", "c1", sql=SQL, title="t")], [TextChunk("ok")]]
    await collect(agent, "first", conv)
    return agent.store.result_tables(conv.id)[0]


@pytest.mark.parametrize("tool", ["show_table", "show_chart"])
async def test_show_tools_refuse_another_conversations_result_table(make_agent, tool):
    llm = ScriptedLLM()
    agent = await make_agent(llm)
    mine_elsewhere = await first_conversation_table(agent, llm)

    def args(name):
        if tool == "show_table":
            return {"result_table": name}
        return {"vega_lite_spec": {"mark": "bar", "data": {"name": name}, "encoding": {"x": {"field": "dport"}}}}

    llm.steps = [[tool_call(tool, "c1", **args(mine_elsewhere))], [tool_call(tool, "c2", **args("rt_deadbeef"))],
                 [TextChunk("done")]]
    events = await collect(agent, "other conversation", Conversation())
    foreign = tool_msgs(llm.calls[-2])[-1]
    missing = last_tool_result(llm.calls[-1])
    assert foreign["success"] is False
    # indistinguishable from a table that does not exist
    assert foreign["error"] == missing["error"]
    assert mine_elsewhere not in json.dumps(foreign)
    assert not [e for e in events if isinstance(e, DisplayEvent)]


async def test_show_table_still_works_for_own_table(make_agent):
    llm = ScriptedLLM(
        [tool_call("run_query", "c1", sql=SQL, title="t")],
        lambda msgs: [tool_call("show_table", "c2", result_table=last_tool_result(msgs)["result_table"])],
        [TextChunk("ok")],
    )
    events = await collect(await make_agent(llm), "q")
    assert len([e for e in events if isinstance(e, DisplayEvent)]) == 1


# --- cancellation and lifecycle ------------------------------------------------------------------

# Never answers `initialize`; exits when its stdin closes, as a real server does, so teardown
# doesn't wait out the client's 2 s grace period before killing it.
HANGING_SERVER = ["-c", "import sys; sys.stdin.read()  # flood_defender.mcp_server"]


@pytest.mark.subprocess
async def test_cancelling_a_turn_while_a_replacement_server_starts_leaves_no_child(build_agent, monkeypatch):
    import sys

    from mcp import StdioServerParameters

    agent = build_agent(ScriptedLLM([TextChunk("one")], [TextChunk("two")]))
    await agent.start()
    try:
        await collect(agent, "q1")
        await kill_mcp_servers_and_settle(agent)
        monkeypatch.setattr(
            agent, "_server_params", lambda: StdioServerParameters(command=sys.executable, args=HANGING_SERVER)
        )
        turn = asyncio.create_task(collect(agent, "q2"))
        deadline = asyncio.get_running_loop().time() + 10
        while not mcp_children() and asyncio.get_running_loop().time() < deadline:
            await asyncio.sleep(0.02)
        assert mcp_children(), "replacement never started"
        turn.cancel()
        await asyncio.gather(turn, return_exceptions=True)
        assert mcp_children() == []
    finally:
        await agent.aclose()
    assert mcp_children() == []


@pytest.mark.subprocess
async def test_cancelling_connection_start_reaps_the_subprocess():
    import sys

    from mcp import StdioServerParameters

    from flood_defender.mcp_connection import McpConnection, stdio_transport

    conn = McpConnection(stdio_transport(StdioServerParameters(command=sys.executable, args=HANGING_SERVER)))
    task = asyncio.create_task(conn.start())
    deadline = asyncio.get_running_loop().time() + 10
    while not mcp_children() and asyncio.get_running_loop().time() < deadline:
        await asyncio.sleep(0.02)
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    assert mcp_children() == []


@pytest.mark.subprocess
async def test_connection_start_after_aclose_or_twice_fails_clearly(build_agent):
    from flood_defender.mcp_connection import McpConnection

    agent = build_agent(ScriptedLLM())
    conn = McpConnection(agent._transport())
    await conn.start()
    with pytest.raises(RuntimeError, match="already started"):
        await conn.start()
    await conn.aclose()
    with pytest.raises(RuntimeError, match="already started|closed"):
        await conn.start()
    assert mcp_children() == []


@pytest.mark.subprocess
async def test_agent_aclose_cancels_and_awaits_in_flight_turns(build_agent):
    started = asyncio.Event()

    def hang(messages):
        started.set()
        return [tool_call("run_query", "c1", sql=SQL, title="t")]

    agent = build_agent(ScriptedLLM(hang))
    await agent.start()
    gen = agent.run_turn(Conversation(), "q")
    first = asyncio.create_task(gen.__anext__())
    await asyncio.wait_for(started.wait(), 10)
    producers = set(agent._turn_tasks)
    assert producers
    await agent.aclose()
    assert all(t.done() for t in producers)
    first.cancel()
    await asyncio.gather(first, return_exceptions=True)
    assert mcp_children() == []
