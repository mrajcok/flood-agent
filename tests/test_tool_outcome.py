"""The tool outcome codec: what each outcome kind projects to for the LLM (golden strings), the internal
part that no projection reads, and content that will not decode."""
import json

import pytest

from flood_agent import queries
from flood_agent.tool_outcome import (
    UNREADABLE,
    Counted,
    DisplayMade,
    Failed,
    ResultTableMade,
    Stopped,
    TableDescribed,
    decode,
    encode,
    from_query_result,
    project,
)

COLS = [{"name": "src", "type": "TEXT"}, {"name": "n", "type": "INTEGER"}]
NOTE = "Only the first 2 rows are kept."
CASES = [
    (ResultTableMade("r1", COLS, 3, executed_sql="CREATE TABLE results.r1 AS SELECT 1"),
     '{"success": true, "result_table": "r1", "columns": [{"name": "src", "type": "TEXT"}, '
     '{"name": "n", "type": "INTEGER"}], "row_count": 3}'),
    (ResultTableMade("r1", COLS, 2, truncated_to=2, note=NOTE),
     '{"success": true, "result_table": "r1", "columns": [{"name": "src", "type": "TEXT"}, '
     '{"name": "n", "type": "INTEGER"}], "row_count": 2, "truncated_to": 2, '
     '"note": "Only the first 2 rows are kept."}'),
    (Counted(7), '{"success": true, "count": 7}'),
    (TableDescribed("r1", "result", COLS, 3),
     '{"success": true, "name": "r1", "kind": "result", "columns": [{"name": "src", "type": "TEXT"}, '
     '{"name": "n", "type": "INTEGER"}], "row_count": 3}'),
    (DisplayMade({"kind": "table", "result_table": "r1", "columns": ["src"]}),
     '{"success": true, "display": {"kind": "table", "result_table": "r1", "columns": ["src"]}}'),
    (DisplayMade({"kind": "chart", "result_table": "r1", "spec": {"mark": "bar", "data": {"name": "r1"}}}, note="cap"),
     '{"success": true, "display": {"kind": "chart", "result_table": "r1", "spec": {"mark": "bar", '
     '"data": {"name": "r1"}}}, "note": "cap"}'),
    (Failed("Only SELECT is allowed."), '{"success": false, "error": "Only SELECT is allowed."}'),
    (Failed("Bad.", raw_error="no such column: secret_ip", executed_sql="CREATE TABLE x AS SELECT 10.1.2.3"),
     '{"success": false, "error": "Bad."}'),
    (Stopped("Stopped by the user.", raw_error="interrupted"),
     '{"success": false, "stopped": true, "error": "Stopped by the user."}'),
]


@pytest.mark.parametrize("outcome, golden", CASES)
def test_projection_is_the_exact_string_the_llm_gets_today(outcome, golden):
    assert project(outcome) == golden


@pytest.mark.parametrize("outcome, golden", CASES)
def test_encode_then_decode_keeps_everything_including_the_internal_part(outcome, golden):
    assert decode(encode(outcome)) == outcome


def test_internal_part_survives_the_wire_but_not_any_projection():
    outcome = Failed("Bad.", raw_error="literal 10.1.2.3", executed_sql="SELECT '10.1.2.3'")
    decoded = decode(encode(outcome))
    assert decoded.raw_error == "literal 10.1.2.3" and decoded.executed_sql == "SELECT '10.1.2.3'"
    assert "10.1.2.3" not in project(decoded)


def test_a_field_no_projection_names_never_reaches_the_llm():
    for outcome, golden in CASES:
        wire = json.loads(encode(outcome))
        wire["debug"] = "SECRET"
        wire["internal"]["debug"] = "SECRET"
        assert project(decode(json.dumps(wire))) == golden


@pytest.mark.parametrize("text", [
    "not json", "[1, 2]", "{}", '{"outcome": "nope"}', '{"outcome": "counted"}', '{"outcome": "counted", "count": "7"}',
    '{"outcome": "failed", "error": 5}', '{"outcome": "display_made", "display": {"kind": "table"}}', "", '{"success": true, "count": 3}',
])
def test_content_that_will_not_decode_is_a_generic_failure_that_keeps_the_raw_text(text):
    outcome = decode(text)
    assert outcome == Failed(UNREADABLE, raw_content=text)
    assert project(outcome) == json.dumps({"success": False, "error": "The tool returned an unreadable result."})


@pytest.mark.parametrize("sql, max_rows", [
    ("SELECT 1 AS a, 'x' AS b, 2.5 AS c, NULL AS d", 100),  # computed columns, every type incl. all-NULL
    ("SELECT 1 AS a UNION ALL SELECT 2 UNION ALL SELECT 3", 2),  # truncated: truncated_to and note
    ("SELECT 1 AS a WHERE 0", 1),  # no rows
])
def test_real_run_query_output_decodes_to_the_same_outcome(telemetry_db, tmp_path, sql, max_rows):
    result = queries.run_query(telemetry_db, tmp_path / "results.db", sql, 5.0, max_rows)
    assert result["success"]
    outcome = from_query_result(result)
    assert decode(encode(outcome)) == outcome


def test_real_run_query_failures_decode_to_the_same_outcome(telemetry_db, tmp_path):
    for sql in ("SELECT * FROM no_such_table", "DROP TABLE flows"):
        outcome = from_query_result(queries.run_query(telemetry_db, tmp_path / "results.db", sql, 5.0, 10))
        assert isinstance(outcome, Failed)
        assert decode(encode(outcome)) == outcome
