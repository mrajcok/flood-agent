"""Query safety and limits (ticket #5), tested through the Agent turn seam."""

import sqlite3

import pytest

from flood_defender import queries
from flood_defender.agent import Conversation, TextDelta, TraceEntry, TurnEnd
from flood_defender.llm import TextChunk

from conftest import ScriptedLLM, all_telemetry_values, collect, last_tool_result, query_limits, tool_call


async def run_one(make_agent, sql, **limits):
    llm = ScriptedLLM([tool_call("run_query", sql=sql, title="t")], [TextChunk("ok")])
    events = await collect(make_agent(llm, **limits), "q")
    return last_tool_result(llm.calls[1]), events


def bad_call(i):
    return [tool_call("run_query", call_id=f"c{i}", sql="SELECT nope FROM flows", title="t")]


def text_of(events):
    return "".join(e.text for e in events if isinstance(e, TextDelta))


@pytest.mark.parametrize(
    "sql",
    [
        "sElEcT 1; dRoP table flows",
        "WITH t AS (SELECT 1) DELETE FROM flows",
        "WITH t AS (SELECT 1 AS a) INSERT INTO flows SELECT * FROM flows",
        "WITH t AS (SELECT 1) UPDATE flows SET bytes = 0",
        "/* harmless */ DROP TABLE flows",
        "-- comment\nDROP TABLE flows",
        "SELECT 1 /* ; */; DELETE FROM flows",
        "SELECT 1; -- c\n SELECT 2",
        "SELECT 1;;",
        "VACUUM",
        "REPLACE INTO flows SELECT * FROM flows",
        "ALTER TABLE flows ADD COLUMN x",
        "  attach database 'x.db' as x",
        "pragma writable_schema=1",
        "SELECT * FROM flows WHERE sip = 'a'; DROP TABLE flows",
        "",
        "   ;  ",
    ],
)
async def test_unsafe_sql_rejected_and_telemetry_intact(make_agent, telemetry_db, sql):
    before = all_telemetry_values(telemetry_db)
    meta, _ = await run_one(make_agent, sql)
    assert meta["success"] is False
    assert "error" in meta
    assert all_telemetry_values(telemetry_db) == before


@pytest.mark.parametrize(
    "sql",
    [
        "select 'a;b' as x from flows",
        "SELECT 'DROP TABLE flows' AS s",
        "SELECT 1 -- ; drop table flows\n",
        "/* ; delete */ SELECT 1",
        "SELECT 1;",
        "WITH t AS (SELECT dport AS d FROM flows) SELECT d FROM t",
        'SELECT 1 AS "drop"',
        "SELECT replace('abc', 'b', 'x') AS r",
        "SELECT 1 UNION SELECT 2",
    ],
)
async def test_legitimate_selects_still_accepted(make_agent, sql):
    meta, _ = await run_one(make_agent, sql)
    assert meta["success"] is True, meta


async def test_sanitised_errors_contain_no_data_literals(make_agent):
    sqls = [
        "SELECT bogus FROM flows WHERE sip = '172.30.41.7' AND bytes = 987654",
        "SELECT * FROM nosuch_table WHERE circuit = 'QX7ZK2M9PA41'",
        "SELECT 'unterminated FROM flows 198.18.77.200",
        "SELECT sip FROM flows WHERE sip = 172.30.41.7",
        "SELECT 987654 FROM",
    ]
    for sql in sqls:
        meta, _ = await run_one(make_agent, sql)
        assert meta["success"] is False
        err = meta["error"]
        assert "raw_error" not in meta
        for leak in ["172.30.41.7", "987654", "QX7ZK2M9PA41", "198.18.77.200", "unterminated"]:
            assert leak not in err, (sql, err)
        assert not any(ch.isdigit() for ch in err), err


async def test_raw_error_kept_for_trace_but_never_sent_to_llm(make_agent):
    sql = "SELECT sip FROM flows WHERE sip = 172.30.41.7"
    llm = ScriptedLLM([tool_call("run_query", sql=sql, title="t")], [TextChunk("ok")])
    events = await collect(make_agent(llm), "q")
    entries = [e for e in events if isinstance(e, TraceEntry) and e.kind == "query_error"]
    assert len(entries) == 1
    e = entries[0]
    assert "near" in e.raw_error
    assert e.sql == sql
    assert e.sanitized_error == last_tool_result(llm.calls[1])["error"]
    assert e.raw_error != e.sanitized_error
    assert e.raw_error not in llm.all_sent_text()


async def test_three_failed_attempts_end_turn_with_clear_message(make_agent):
    llm = ScriptedLLM(bad_call(1), bad_call(2), bad_call(3), [TextChunk("SHOULD NOT BE REACHED")])
    events = await collect(make_agent(llm), "q")
    text = text_of(events)
    assert "couldn't build" in text.lower() and "rephras" in text.lower()
    assert "SHOULD NOT" not in text
    assert isinstance(events[-1], TurnEnd)
    assert len(llm.calls) == 3


async def test_two_failures_then_success_is_fine(make_agent):
    llm = ScriptedLLM(
        bad_call(1),
        bad_call(2),
        [tool_call("run_query", call_id="c3", sql="SELECT 1 AS a", title="t")],
        [TextChunk("done")],
    )
    events = await collect(make_agent(llm), "q")
    assert text_of(events) == "done"


async def test_failure_budget_resets_each_turn(make_agent):
    llm = ScriptedLLM(bad_call(1), bad_call(2), [TextChunk("a")], bad_call(3), bad_call(4), [TextChunk("b")])
    agent = make_agent(llm)
    conv = Conversation()
    for expected in ("a", "b"):
        events = await collect(agent, "q", conv)
        assert text_of(events) == expected


async def test_failure_limit_is_configurable(make_agent):
    llm = ScriptedLLM(bad_call(1), [TextChunk("unreached")])
    events = await collect(make_agent(llm, max_failed_attempts=1), "q")
    assert "couldn't build" in text_of(events).lower()


async def test_query_timeout_aborts_with_guidance(make_agent):
    sql = "WITH RECURSIVE c(x) AS (SELECT 1 UNION ALL SELECT x+1 FROM c) SELECT COUNT(*) FROM c"
    meta, _ = await run_one(make_agent, sql, query_timeout_s=0.3)
    assert meta["success"] is False
    assert "time" in meta["error"].lower()
    assert "filter" in meta["error"].lower() or "aggregate" in meta["error"].lower()


async def test_row_cap_refuses_with_guidance_and_leaves_no_table(make_agent, tmp_path):
    meta, _ = await run_one(make_agent, "SELECT * FROM flows", max_result_rows=2)
    assert meta["success"] is False
    assert "result_table" not in meta
    assert "aggregate" in meta["error"].lower() or "filter" in meta["error"].lower()
    con = sqlite3.connect(tmp_path / "results.db")
    assert con.execute("SELECT COUNT(*) FROM sqlite_master WHERE type='table'").fetchone()[0] == 0


async def test_result_exactly_at_cap_is_allowed(make_agent):
    meta, _ = await run_one(make_agent, "SELECT dport FROM flows LIMIT 2", max_result_rows=2)
    assert meta["success"] is True and meta["row_count"] == 2


def test_telemetry_database_is_unwritable_even_if_validation_bypassed(telemetry_db, tmp_path):
    con = queries.open_connection(telemetry_db, tmp_path / "results.db")
    try:
        for stmt in [
            "DELETE FROM flows",
            "DROP TABLE flows",
            "UPDATE flows SET bytes = 0",
            "CREATE TABLE main.evil (a)",
            "ATTACH DATABASE ':memory:' AS evil",
            "PRAGMA writable_schema = 1",
        ]:
            with pytest.raises(sqlite3.Error):
                con.execute(stmt)
    finally:
        con.close()
    assert all_telemetry_values(telemetry_db) >= {"172.30.41.7"}


def test_bypassed_validation_cannot_write_via_run_query(telemetry_db, tmp_path, monkeypatch):
    monkeypatch.setattr(queries, "validate_select", lambda sql: sql)
    for sql in ["SELECT 1; DELETE FROM flows", "SELECT 1) ; DELETE FROM flows; --"]:
        res = queries.run_query(telemetry_db, tmp_path / "results.db", sql, **query_limits())
        assert res["success"] is False
    assert len(all_telemetry_values(telemetry_db)) > 10
