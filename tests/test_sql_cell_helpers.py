"""SQL Cell helpers: a table's columns for click-to-insert, and the hint for a keyword column (HTTP seam)."""

import threading

import pytest
from conftest import ScriptedLLM

from flood_agent import queries
from flood_agent.catalog import CellRun


def test_a_telemetry_tables_columns_come_with_types_and_the_text_to_insert(build_agent, web_client):
    client = web_client(build_agent(ScriptedLLM()))
    r = client.get("/api/tables/flows/columns")
    assert r.status_code == 200
    cols = {c["name"]: c for c in r.json()["columns"]}
    assert cols["dport"] == {"name": "dport", "type": "INTEGER", "insert": "dport"}
    assert list(cols)[:3] == ["sip", "dip", "sport"]


def test_an_unknown_table_has_no_columns(build_agent, web_client):
    client = web_client(build_agent(ScriptedLLM()))
    assert client.get("/api/tables/nope/columns").status_code == 404
    assert client.get("/api/tables/sqlite_master/columns").status_code == 404


def test_a_result_tables_columns_are_listed_and_a_keyword_or_odd_name_is_quoted(build_agent, web_client):
    agent = build_agent(ScriptedLLM())
    client = web_client(agent)
    sql = 'SELECT dport AS "analyze", dport AS "my col", dport AS "select", dport AS ok FROM flows'
    outcome = agent._execute_sql(sql, 10, threading.Event(), None, CellRun("c", "r"))
    cols = {c["name"]: c["insert"] for c in client.get(f"/api/tables/{outcome['table']}/columns").json()["columns"]}
    assert cols == {"analyze": '"analyze"', "my col": '"my col"', "select": '"select"', "ok": "ok"}


@pytest.mark.parametrize("sql", ["SELECT analyze FROM flows", "SELECT sip, update FROM flows"])
def test_a_keyword_column_rejection_suggests_quoting_it(sql):
    with pytest.raises(queries.QueryRejected, match="double quotes"):
        queries.validate_select(sql)


def test_other_rejections_do_not_mention_quoting():
    with pytest.raises(queries.QueryRejected) as e:
        queries.validate_select("SHOW TABLES")
    assert "quotes" not in str(e.value)
