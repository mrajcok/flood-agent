"""Result Table names: rt_<YYYYMMDD>_<HHMMSS>[_<n>], valid unquoted in SQLite and Ocient."""

import re
import sqlite3

from conftest import ScriptedLLM, collect, query_limits, tool_call

from flood_agent import queries, table_names
from flood_agent.agent import Conversation
from flood_agent.llm import TextChunk

SQL = "SELECT dport, SUM(bytes) AS total_bytes FROM flows GROUP BY dport"
NAME = re.compile(r"rt_\d{8}_\d{6}(_\d+)?")


def run(telemetry_db, tmp_path, title):
    return queries.run_query(telemetry_db, tmp_path / "results.db", SQL, title=title, **query_limits())


def test_name_ignores_the_title(telemetry_db, tmp_path):
    for title in ["Top 10 dest IPs", "évil_name; DROP TABLE flows", ""]:
        meta = run(telemetry_db, tmp_path, title)
        name = meta["result_table"]
        assert NAME.fullmatch(name) and table_names.GENERATED_RE.fullmatch(name), name
        assert f"results.{name} AS" in meta["executed_sql"]


def test_two_tables_in_the_same_second_get_distinct_names(telemetry_db, tmp_path, monkeypatch):
    monkeypatch.setattr(queries, "_timestamp", lambda: "20261101_230402")
    a = run(telemetry_db, tmp_path, "Top ports")["result_table"]
    b = run(telemetry_db, tmp_path, "Other title")["result_table"]
    c = run(telemetry_db, tmp_path, "Top ports")["result_table"]
    assert (a, b, c) == ("rt_20261101_230402", "rt_20261101_230402_2", "rt_20261101_230402_3")
    con = sqlite3.connect(tmp_path / "results.db")
    assert con.execute(f"SELECT COUNT(*) FROM {c}").fetchone()[0] > 0


async def test_llm_title_still_appears_in_the_system_prompt(make_agent):
    llm = ScriptedLLM(
        [tool_call("run_query", sql=SQL, title="Top ports by bytes")],
        [TextChunk("done")],
        [TextChunk("again")],
    )
    agent = await make_agent(llm)
    conversation = Conversation()
    await collect(agent, "q", conversation)
    await collect(agent, "q2", conversation)
    system = llm.calls[2][0]["content"]
    assert "Top ports by bytes" in system and "rt_" in system
