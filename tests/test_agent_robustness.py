"""Agent turn robustness: bad LLM tool arguments, partial failures, atomic history, MCP
subprocess teardown, Result Table ownership in show_*. Real MCP subprocess, scripted LLM."""

import json
import os

import pytest

import flood_defender.agent as agent_mod
from flood_defender.agent import Conversation, DisplayEvent, TextDelta, TraceReady, TurnEnd, TurnError
from flood_defender.llm import LLMError, TextChunk, ToolCallChunk

from conftest import ScriptedLLM, collect, last_tool_result, tool_call

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
    events = await collect(make_agent(llm), "q")
    assert isinstance(events[-1], TurnEnd) and text_of(events) == "recovered"
    first = last_tool_result(llm.calls[1])
    assert first["success"] is False and "JSON" in first["error"]
    assert last_tool_result(llm.calls[2])["success"] is True


async def test_non_object_arguments_are_an_error_result(make_agent):
    llm = ScriptedLLM([ToolCallChunk("c1", "show_table", "[1, 2]")], [TextChunk("ok")])
    events = await collect(make_agent(llm), "q")
    assert isinstance(events[-1], TurnEnd)
    assert last_tool_result(llm.calls[1])["success"] is False


async def test_run_query_without_sql_is_an_error_result_and_counts_as_a_failed_attempt(make_agent):
    llm = ScriptedLLM(
        [tool_call("run_query", "c1", title="t")],
        [tool_call("run_query", "c2", title="t")],
        [tool_call("run_query", "c3", title="t")],
    )
    events = await collect(make_agent(llm), "q")
    r = last_tool_result(llm.calls[1])
    assert r["success"] is False and "sql" in r["error"]
    assert agent_mod.GIVE_UP_MESSAGE in text_of(events)
    assert isinstance(events[-1], TurnEnd)


# --- items 1, 7: failures partway through a turn ------------------------------------------------


async def test_failed_llm_step_keeps_history_valid_surfaces_error_and_saves_trace(make_agent):
    def boom(messages):
        raise LLMError("The model provider reported an error: upstream exploded")

    llm = ScriptedLLM([tool_call("run_query", "c1", sql=SQL, title="t")], boom)
    agent = make_agent(llm)
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

    agent = make_agent(ScriptedLLM(boom))
    conv = Conversation()
    events = await collect(agent, "q", conv)
    (err,) = [e for e in events if isinstance(e, TurnError)]
    assert "RuntimeError" in err.detail and "kaboom" in err.detail
    assert isinstance(events[-1], TurnEnd)
    assert any(t["type"] == "trace" for t in agent.store.transcript(conv.id))


async def test_open_conversation_repairs_corrupt_saved_history(make_agent):
    agent = make_agent(ScriptedLLM())
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


async def test_steps_exhausted_tells_the_user_and_saves_the_trace(make_agent, monkeypatch):
    monkeypatch.setattr(agent_mod, "MAX_LLM_STEPS", 2)
    llm = ScriptedLLM(
        [tool_call("run_query", "c1", sql=SQL, title="t")],
        [tool_call("run_query", "c2", sql=SQL, title="t")],
    )
    agent = make_agent(llm)
    conv = Conversation()
    events = await collect(agent, "q", conv)
    assert agent_mod.STEP_LIMIT_MESSAGE in text_of(events)
    assert any(isinstance(e, TraceReady) for e in events) and isinstance(events[-1], TurnEnd)
    saved = agent.store.messages(conv.id)
    assert saved[-1] == {"role": "assistant", "content": agent_mod.STEP_LIMIT_MESSAGE}
    assert_valid_history(saved)


# --- item 3: no orphaned MCP subprocess ---------------------------------------------------------


def mcp_children() -> list[int]:
    me, found = os.getpid(), []
    for pid in filter(str.isdigit, os.listdir("/proc")):
        try:
            with open(f"/proc/{pid}/stat") as f:
                ppid = int(f.read().rsplit(")", 1)[1].split()[1])
            if ppid != me:
                continue
            with open(f"/proc/{pid}/cmdline", "rb") as f:
                if b"flood_defender.mcp_server" in f.read():
                    found.append(int(pid))
        except (OSError, IndexError, ValueError):
            continue  # process vanished
    return found


async def test_no_mcp_subprocess_left_after_a_turn(make_agent):
    llm = ScriptedLLM([tool_call("run_query", "c1", sql=SQL, title="t")], [TextChunk("done")])
    await collect(make_agent(llm), "q")
    assert mcp_children() == []


async def test_no_mcp_subprocess_left_after_a_failed_turn(make_agent):
    def boom(messages):
        raise RuntimeError("x")

    await collect(make_agent(ScriptedLLM(boom)), "q")
    assert mcp_children() == []


async def test_no_mcp_subprocess_left_when_the_consumer_stops_early(make_agent):
    llm = ScriptedLLM([TextChunk("partial"), tool_call("run_query", "c1", sql=SQL, title="t")], [TextChunk("x")])
    gen = make_agent(llm).run_turn(Conversation(), "q")
    async for event in gen:
        if isinstance(event, TextDelta):
            break
    assert mcp_children() != []  # the MCP server really was running when we stopped
    await gen.aclose()
    assert mcp_children() == []


# --- item 4: show_table / show_chart ownership ---------------------------------------------------


async def first_conversation_table(agent, llm):
    conv = Conversation()
    llm.steps = [[tool_call("run_query", "c1", sql=SQL, title="t")], [TextChunk("ok")]]
    await collect(agent, "first", conv)
    return agent.store.result_tables(conv.id)[0]


@pytest.mark.parametrize("tool", ["show_table", "show_chart"])
async def test_show_tools_refuse_another_conversations_result_table(make_agent, tool):
    llm = ScriptedLLM()
    agent = make_agent(llm)
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
    events = await collect(make_agent(llm), "q")
    assert len([e for e in events if isinstance(e, DisplayEvent)]) == 1
