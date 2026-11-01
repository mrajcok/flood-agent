"""run_count_query: returns one count to the LLM, creates no Result Table."""

import pytest

from flood_defender import queries


def _run(telemetry_db, tmp_path, sql):
    return queries.run_count_query(telemetry_db, tmp_path / "results.db", sql, 5.0)


def test_returns_the_count(telemetry_db, tmp_path):
    assert _run(telemetry_db, tmp_path, "SELECT COUNT(*) FROM flows") == {"success": True, "count": 3}


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT bytes FROM flows LIMIT 1",  # not a count
        "SELECT COUNT(*), 1 FROM flows",  # two columns
        "SELECT COUNT(*) FROM flows GROUP BY dport",  # several rows
        "SELECT sip || COUNT(*) FROM flows",  # not an integer
        "DROP TABLE flows",
    ],
)
def test_rejects_anything_but_one_integer_count(telemetry_db, tmp_path, sql):
    result = _run(telemetry_db, tmp_path, sql)
    assert result["success"] is False
    assert "count" not in result


def test_sqlite_error_keeps_raw_error(telemetry_db, tmp_path):
    result = _run(telemetry_db, tmp_path, "SELECT COUNT(*) FROM nope")
    assert result["success"] is False and "raw_error" in result


async def test_agent_turn_gives_llm_the_count_and_creates_no_result_table(make_agent):
    from conftest import ScriptedLLM, collect, last_tool_result, tool_call

    from flood_defender.llm import TextChunk

    llm = ScriptedLLM(
        [tool_call("run_count_query", sql="SELECT COUNT(*) FROM flows")], [TextChunk("There are 3 flows.")]
    )
    agent = await make_agent(llm)
    events = await collect(agent, "how many flows?")
    assert "run_count_query" in llm.tools_seen_names
    assert last_tool_result(llm.calls[1]) == {"success": True, "count": 3}
    assert not any(type(e).__name__ == "DisplayEvent" for e in events)


async def test_failed_count_queries_count_toward_the_retry_limit(make_agent):
    from conftest import ScriptedLLM, collect, text_of, tool_call

    bad = [[tool_call("run_count_query", call_id=f"c{i}", sql="SELECT sip FROM flows")] for i in range(3)]
    llm = ScriptedLLM(*bad, [])
    text = text_of(await collect(await make_agent(llm), "q"))
    assert "couldn't build" in text.lower()
    assert "raw_error" not in llm.all_sent_text()


def test_chart_template_fills_the_pane_width():
    from pathlib import Path

    tpl = (Path(__file__).parent.parent / "src/flood_defender/templates/display_chart.html").read_text()
    assert "width: 'container'" in tpl and "fit-x" in tpl
