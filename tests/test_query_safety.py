"""Query safety and limits (ticket #5), tested through the Agent turn seam."""

import json
import sqlite3

import pytest
from conftest import ScriptedLLM, all_telemetry_values, collect, last_tool_result, query_limits, tool_call

from flood_agent import queries
from flood_agent.agent import Conversation, TextDelta, TraceEntry, TurnEnd
from flood_agent.llm import TextChunk, ToolCallChunk


async def run_one(make_agent, sql, max_result_rows=1000, **limits):
    llm = ScriptedLLM([tool_call("run_query", sql=sql, title="t")], [TextChunk("ok")])
    events = await collect(await make_agent(llm, **limits), "q", max_result_rows=max_result_rows)
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
    events = await collect(await make_agent(llm), "q")
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
    events = await collect(await make_agent(llm), "q")
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
    events = await collect(await make_agent(llm), "q")
    assert text_of(events) == "done"


async def test_failure_budget_resets_each_turn(make_agent):
    llm = ScriptedLLM(bad_call(1), bad_call(2), [TextChunk("a")], bad_call(3), bad_call(4), [TextChunk("b")])
    agent = await make_agent(llm)
    conv = Conversation()
    for expected in ("a", "b"):
        events = await collect(agent, "q", conv)
        assert text_of(events) == expected


async def test_failure_limit_is_configurable(make_agent):
    llm = ScriptedLLM(bad_call(1), [TextChunk("unreached")])
    events = await collect(await make_agent(llm, max_failed_attempts=1), "q")
    assert "couldn't build" in text_of(events).lower()


async def test_query_timeout_aborts_with_guidance(make_agent):
    sql = "WITH RECURSIVE c(x) AS (SELECT 1 UNION ALL SELECT x+1 FROM c) SELECT COUNT(*) FROM c"
    meta, _ = await run_one(make_agent, sql, query_timeout_s=0.3)
    assert meta["success"] is False
    assert "time" in meta["error"].lower()
    assert "filter" in meta["error"].lower() or "aggregate" in meta["error"].lower()


async def test_row_cap_keeps_the_first_rows_and_flags_the_table_truncated(make_agent, tmp_path):
    meta, _ = await run_one(make_agent, "SELECT * FROM flows", max_result_rows=2)
    assert meta["success"] is True
    assert meta["row_count"] == 2 and meta["truncated_to"] == 2
    assert "truncated" in meta["note"].lower()
    con = sqlite3.connect(tmp_path / "results.db")
    assert con.execute(f"SELECT COUNT(*) FROM {meta['result_table']}").fetchone()[0] == 2


async def test_result_below_the_cap_is_not_flagged(make_agent):
    meta, _ = await run_one(make_agent, "SELECT dport FROM flows LIMIT 2", max_result_rows=3)
    assert meta["success"] is True and meta["row_count"] == 2
    assert "truncated_to" not in meta and "note" not in meta


async def test_result_exactly_at_the_cap_is_assumed_truncated(make_agent):
    # We cannot tell "exactly the cap" from "more than the cap" without fetching an extra row.
    meta, _ = await run_one(make_agent, "SELECT dport FROM flows LIMIT 2", max_result_rows=2)
    assert meta["success"] is True and meta["row_count"] == 2 and meta["truncated_to"] == 2


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


SERIES_OF_5 = "WITH RECURSIVE n(i) AS (SELECT 1 UNION ALL SELECT i + 1 FROM n WHERE i < 5) SELECT i FROM n"


async def _run_with_turn_cap(make_agent, sql, turn_cap, **limits):
    llm = ScriptedLLM([tool_call("run_query", sql=sql, title="t")], [TextChunk("ok")])
    agent = await make_agent(llm, **limits)
    await collect(agent, "q", max_result_rows=turn_cap)
    return last_tool_result(llm.calls[1])


async def test_each_turn_uses_its_own_row_cap(make_agent):
    small = await _run_with_turn_cap(make_agent, SERIES_OF_5, 2)
    assert small["row_count"] == 2 and small["truncated_to"] == 2
    large = await _run_with_turn_cap(make_agent, SERIES_OF_5, 6)
    assert large["row_count"] == 5 and "truncated_to" not in large


TYPED_SQL = ("SELECT dport, SUM(bytes) AS total_bytes, AVG(bytes) AS avg_bytes, NULL AS no_value, "
             "CAST(dport AS TEXT) AS port_text FROM flows GROUP BY dport")
EXPECTED_TYPES = {"dport": "INT",  # declared types are kept (CREATE TABLE AS declares INT)
                  "total_bytes": "INTEGER", "avg_bytes": "REAL", "no_value": "ANY", "port_text": "TEXT"}


async def test_computed_columns_report_the_type_of_their_values_not_any(make_agent):
    """SQLite gives computed columns no declared type; the metadata names the stored values' type
    instead, so the LLM can pick quantitative or nominal (ANY only when every value is NULL)."""
    llm = ScriptedLLM(
        [tool_call("run_query", sql=TYPED_SQL, title="t")],
        lambda msgs: [ToolCallChunk("c2", "describe_table", json.dumps({"name": last_tool_result(msgs)["result_table"]}))],
        [TextChunk("ok")],
    )
    await collect(await make_agent(llm), "q")
    ran, described = last_tool_result(llm.calls[1]), last_tool_result(llm.calls[2])
    assert {c["name"]: c["type"] for c in ran["columns"]} == EXPECTED_TYPES
    assert {c["name"]: c["type"] for c in described["columns"]} == EXPECTED_TYPES
