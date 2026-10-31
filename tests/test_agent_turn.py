"""Agent turn tests: MCP server in-process, real SQLite files, scripted fake LLM."""

import sqlite3

import pytest
from conftest import ScriptedLLM, all_telemetry_values, collect, last_tool_result, tool_call

from flood_defender.agent import Conversation, DisplayEvent, TextDelta, TurnEnd
from flood_defender.llm import TextChunk

TOP_PORTS_SQL = "SELECT dport, SUM(bytes) AS total_bytes FROM flows GROUP BY dport ORDER BY total_bytes DESC"


def top_ports_script():
    return ScriptedLLM(
        [tool_call("run_query", sql=TOP_PORTS_SQL, title="Top ports")],
        lambda msgs: [
            tool_call("show_table", result_table=last_tool_result(msgs)["result_table"])
        ],
        [TextChunk("Here is the table "), TextChunk("of top ports.")],
    )


async def test_question_streams_text_and_yields_table_display_with_rows(make_agent):
    agent = await make_agent(top_ports_script())
    events = await collect(agent, "top destination ports by bytes?")

    text = "".join(e.text for e in events if isinstance(e, TextDelta))
    assert text == "Here is the table of top ports."
    assert isinstance(events[-1], TurnEnd)

    displays = [e for e in events if isinstance(e, DisplayEvent)]
    assert len(displays) == 1
    d = displays[0]
    assert d.kind == "table"
    data = agent.fetch_rows(d.result_table)
    assert data["columns"] == ["dport", "total_bytes"]
    assert data["rows"] == [[8443, 1_111_111], [9053, 555_321]]


async def test_run_query_returns_only_query_metadata(make_agent):
    llm = top_ports_script()
    await collect(await make_agent(llm), "q")
    meta = last_tool_result(llm.calls[1])
    assert set(meta) == {"success", "result_table", "columns", "row_count"}
    assert meta["success"] is True
    assert meta["row_count"] == 2
    assert [c["name"] for c in meta["columns"]] == ["dport", "total_bytes"]


async def test_result_table_lives_in_results_db_with_tool_chosen_name(make_agent, telemetry_db, tmp_path):
    llm = ScriptedLLM(
        [tool_call("run_query", sql=TOP_PORTS_SQL, title="evil_name; DROP TABLE flows")],
        [TextChunk("done")],
    )
    await collect(await make_agent(llm), "q")
    name = last_tool_result(llm.calls[1])["result_table"]
    assert name.startswith("rt_") and "evil" not in name

    results = sqlite3.connect(tmp_path / "results.db")
    assert results.execute(f"SELECT COUNT(*) FROM {name}").fetchone()[0] == 2
    tele = sqlite3.connect(telemetry_db)
    tables = {r[0] for r in tele.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert tables == {"flows"}


@pytest.mark.parametrize(
    "sql",
    [
        "DELETE FROM flows",
        "DROP TABLE flows",
        "SELECT 1; DROP TABLE flows",
        "ATTACH DATABASE 'x.db' AS x",
        "PRAGMA writable_schema = 1",
        "CREATE TABLE t AS SELECT 1",
    ],
)
async def test_unsafe_sql_is_rejected_and_telemetry_untouched(make_agent, telemetry_db, sql):
    llm = ScriptedLLM([tool_call("run_query", sql=sql, title="t")], [TextChunk("ok")])
    await collect(await make_agent(llm), "q")
    meta = last_tool_result(llm.calls[1])
    assert meta["success"] is False
    assert all_telemetry_values(telemetry_db) >= {"172.30.41.7"}


async def test_failed_query_error_is_returned_without_data(make_agent):
    llm = ScriptedLLM(
        [tool_call("run_query", sql="SELECT nope FROM flows", title="t")], [TextChunk("sorry")]
    )
    await collect(await make_agent(llm), "q")
    meta = last_tool_result(llm.calls[1])
    assert meta["success"] is False
    assert "error" in meta


async def test_only_run_query_show_table_and_show_chart_are_llm_tools(make_agent):
    llm = ScriptedLLM([TextChunk("hi")])
    await collect(await make_agent(llm), "hello")
    assert sorted(llm.tools_seen_names) == ["run_query", "show_chart", "show_table"]


async def test_system_prompt_contains_semantic_layer_and_now(make_agent):
    llm = ScriptedLLM([TextChunk("hi")])
    await collect(await make_agent(llm), "hello")
    system = llm.calls[0][0]
    assert system["role"] == "system"
    assert "etime" in system["content"] and "1600000100000" in system["content"]


async def test_second_turn_keeps_conversation_history(make_agent):
    llm = ScriptedLLM([TextChunk("one")], [TextChunk("two")])
    agent = await make_agent(llm)
    conv = Conversation()
    await collect(agent, "first", conv)
    await collect(agent, "second", conv)
    roles = [m["role"] for m in llm.calls[1]]
    assert roles == ["system", "user", "assistant", "user"]


async def test_no_telemetry_value_ever_reaches_the_llm(make_agent, telemetry_db):
    scripts = [
        top_ports_script(),
        ScriptedLLM(
            [tool_call("run_query", sql="SELECT sip, circuit, etime, bytes FROM flows", title="raw")],
            lambda msgs: [tool_call("show_table", result_table=last_tool_result(msgs)["result_table"])],
            [TextChunk("shown")],
        ),
        ScriptedLLM(
            [tool_call("run_query", sql="SELECT bogus FROM flows WHERE sip = '172.30.41.7'", title="x")],
            [TextChunk("failed")],
        ),
        ScriptedLLM(
            [tool_call("run_query", sql="DELETE FROM flows WHERE sip='172.30.41.7'", title="x")],
            [TextChunk("refused")],
        ),
    ]
    values = {v for v in all_telemetry_values(telemetry_db) if len(v) >= 4}
    for llm in scripts:
        await collect(await make_agent(llm), "anything")
        # Everything the Agent put into the conversation (system prompt, tool
        # results) must be value-free. Assistant messages are the LLM's own words.
        for call in llm.calls:
            for m in call:
                if m["role"] in ("system", "tool"):
                    for v in values:
                        assert v not in m["content"], (v, m["role"])
