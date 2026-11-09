"""Refresh a Result Table or Saved Table: POST /api/results/{name}/refresh runs its stored query again,
without the LLM, and replaces the table under the same name (HTTP seam, ADR 0006)."""

import sqlite3
import threading
from contextlib import closing
from dataclasses import replace

from conftest import DAY, Clock, ScriptedLLM, last_tool_result, tool_call

from flood_agent import queries
from flood_agent.llm import TextChunk

HOUR = 3_600_000
RECENT = "SELECT sip, etime FROM flows WHERE etime > now() - 1500 ORDER BY etime"  # 2 rows at 1700000003000, 1 later


def ask(build_agent, web_client, *sqls, max_rows="1000"):
    """Run a turn whose LLM makes one run_query per SQL (a SQL may be a function of the earlier result's
    name). Returns (client, agent, clock, llm, [table names])."""
    clock = Clock()
    steps = [[tool_call("run_query", sql=sql, title="T")] if isinstance(sql, str)
             else (lambda msgs, sql=sql: [tool_call("run_query", sql=sql(last_tool_result(msgs)["result_table"]), title="T")])
             for sql in sqls]
    llm = ScriptedLLM(*steps, [TextChunk("done")])
    agent = build_agent(llm, clock=clock, result_ttl_ms=DAY)
    client = web_client(agent)
    posted = client.post("/chat", data={"message": "go", "conversation_id": "", "max_rows": max_rows})
    client.get(posted.text.split('sse-connect="')[1].split('"')[0])
    return client, agent, clock, llm, agent.tables.names()


def rows(client, name):
    return client.get(f"/api/results/{name}/rows").json()


def refresh(client, name):
    return client.post(f"/api/results/{name}/refresh")


def result_tables_in_results_db(agent):
    with closing(sqlite3.connect(agent.config.results_db)) as con:
        return sorted(n for (n,) in con.execute("SELECT name FROM sqlite_master WHERE type='table'"))


def test_refresh_replaces_the_rows_under_the_same_name_and_restarts_retention(build_agent, web_client, monkeypatch):
    monkeypatch.setenv("FIXED_NOW_MS", "1700000003000")
    client, agent, clock, _, (name,) = ask(build_agent, web_client, RECENT)
    assert rows(client, name)["total"] == 2
    created = agent.tables[name].conversation_id

    monkeypatch.setenv("FIXED_NOW_MS", "1700000003600")  # the clock moves: only the last flow is recent now
    clock.ms += HOUR
    r = refresh(client, name)

    assert r.status_code == 200
    body = r.json()
    assert body["name"] == name and body["row_count"] == 1 and body["as_of_ms"] == clock.ms
    assert body["retention_days"] == 1 and body["expires_in_ms"] == DAY and body["truncated_to"] is None
    assert [c["name"] for c in body["columns"]] == ["sip", "etime"]
    after = rows(client, name)
    assert after["total"] == 1 and after["rows"] == [["172.30.99.3", 1_700_000_002_999]]
    assert agent.tables[name].conversation_id == created  # ownership is unchanged


def test_as_of_is_the_creation_time_until_the_first_refresh(build_agent, web_client):
    client, _, clock, _, (name,) = ask(build_agent, web_client, RECENT)
    settings = client.get(f"/api/results/{name}/settings").json()
    assert settings["refreshable"] is True and settings["as_of_ms"] == clock.ms
    clock.ms += HOUR
    assert refresh(client, name).json()["as_of_ms"] == clock.ms
    assert client.get(f"/api/results/{name}/settings").json()["as_of_ms"] == clock.ms


def test_a_saved_table_is_refreshed_the_same_way_and_keeps_its_retention(build_agent, web_client, monkeypatch):
    monkeypatch.setenv("FIXED_NOW_MS", "1700000003000")
    client, _, clock, _, (name,) = ask(build_agent, web_client, RECENT)
    client.post(f"/api/results/{name}/settings", data={"name": "recent_flows", "retention_days": "45"})
    clock.ms += 10 * DAY
    monkeypatch.setenv("FIXED_NOW_MS", "1700000003600")

    body = refresh(client, "recent_flows").json()

    assert body["name"] == "recent_flows" and body["row_count"] == 1
    assert body["retention_days"] == 45 and body["expires_in_ms"] == 45 * DAY
    assert rows(client, "recent_flows")["total"] == 1


def test_the_row_cap_is_reused_and_truncation_is_reported_only_while_it_applies(build_agent, web_client, monkeypatch):
    monkeypatch.setenv("FIXED_NOW_MS", "1700000003000")
    client, _, _, _, (name,) = ask(build_agent, web_client, RECENT, max_rows="2")
    assert rows(client, name)["truncated_to"] == 2

    assert refresh(client, name).json()["truncated_to"] == 2  # still cut to 2
    monkeypatch.setenv("FIXED_NOW_MS", "1700000003600")
    body = refresh(client, name).json()
    assert body["row_count"] == 1 and body["truncated_to"] is None
    assert rows(client, name)["truncated_to"] is None


def test_a_failed_refresh_leaves_the_table_exactly_as_it_was(build_agent, web_client):
    client, agent, clock, _, (a, b) = ask(
        build_agent, web_client, "SELECT sip FROM flows", lambda a: f"SELECT sip FROM {a}")
    client.delete(f"/api/tables/{a}")  # the table B was made from has expired
    clock.ms += HOUR
    before_rows, before = rows(client, b), agent.tables[b]

    r = refresh(client, b)

    assert r.status_code == 422 and "expired" in r.json()["detail"].lower()
    assert rows(client, b)["rows"] == before_rows["rows"]
    assert agent.tables[b] == before
    assert result_tables_in_results_db(agent) == [b]  # no temporary table is left behind


def test_a_query_that_times_out_fails_plainly_and_changes_nothing(build_agent, web_client):
    slow = "WITH RECURSIVE c(x) AS (SELECT 1 UNION ALL SELECT x + 1 FROM c WHERE x < 400000) SELECT MAX(x) AS m FROM c"
    client, agent, _, _, (name,) = ask(build_agent, web_client, slow)
    agent.config = replace(agent.config, query_timeout_s=0.01)

    r = refresh(client, name)

    assert r.status_code == 422 and "time limit" in r.json()["detail"]
    assert rows(client, name)["rows"] == [[400000]]
    assert result_tables_in_results_db(agent) == [name]


def test_the_stored_query_is_checked_like_an_llm_query(build_agent, web_client, monkeypatch):
    monkeypatch.setenv("FIXED_NOW_MS", "1700000003000")
    client, agent, _, _, (name,) = ask(build_agent, web_client, RECENT)
    with closing(sqlite3.connect(agent.tables.path)) as con, con:
        con.execute("UPDATE result_tables SET sql='DROP TABLE flows'")

    r = refresh(client, name)

    assert r.status_code == 422 and "SELECT" in r.json()["detail"]
    assert rows(client, name)["total"] == 2


def test_a_second_refresh_while_one_runs_is_a_conflict(build_agent, web_client, monkeypatch):
    client, _, _, _, (name,) = ask(build_agent, web_client, RECENT)
    started, release, real = threading.Event(), threading.Event(), queries.run_query

    def slow(*args, **kwargs):
        started.set()
        assert release.wait(5)
        return real(*args, **kwargs)

    monkeypatch.setattr(queries, "run_query", slow)
    first = []
    t = threading.Thread(target=lambda: first.append(refresh(client, name)))
    t.start()
    assert started.wait(5)
    try:
        assert refresh(client, name).status_code == 409
    finally:
        release.set()
        t.join(5)
    assert first[0].status_code == 200
    monkeypatch.setattr(queries, "run_query", real)
    assert refresh(client, name).status_code == 200  # the lock is released


def test_tables_with_no_stored_query_and_expired_tables_are_not_refreshable(build_agent, web_client, monkeypatch):
    monkeypatch.setenv("FIXED_NOW_MS", "1700000003000")
    client, agent, clock, _, (name,) = ask(build_agent, web_client, RECENT)
    with closing(sqlite3.connect(agent.tables.path)) as con, con:  # as a table made before this feature
        con.execute("UPDATE result_tables SET sql=NULL, max_rows=NULL")
    assert client.get(f"/api/results/{name}/settings").json()["refreshable"] is False
    assert refresh(client, name).status_code == 422
    assert rows(client, name)["total"] == 2

    clock.ms += DAY + 1
    assert refresh(client, name).status_code == 410


def test_lookup_telemetry_and_unknown_tables_are_not_refreshable(build_agent, web_client):
    client, *_ = ask(build_agent, web_client, RECENT)
    client.post("/api/lookup-tables", data={"name": "watch", "rows": "ip\n10.0.0.1"})
    for name in ("flows", "lookup_watch", "nope"):
        assert refresh(client, name).status_code == 404


def test_the_llm_is_told_nothing(build_agent, web_client):
    client, agent, _, llm, (name,) = ask(build_agent, web_client, RECENT)
    (conversation_id,) = [c["id"] for c in agent.store.list_conversations()]
    calls, messages = len(llm.calls), agent.store.messages(conversation_id)

    refresh(client, name)

    assert len(llm.calls) == calls and agent.store.messages(conversation_id) == messages


def test_a_catalog_from_before_this_feature_gains_the_new_columns(tmp_path):
    from flood_agent.catalog import Catalog

    app_db = tmp_path / "app.db"
    with closing(sqlite3.connect(app_db)) as con, con:
        con.executescript("""
            CREATE TABLE conversations(id TEXT PRIMARY KEY, last_activity_at INTEGER);
            CREATE TABLE result_tables (name TEXT PRIMARY KEY, conversation_id TEXT NOT NULL, turn_id INTEGER NOT NULL,
              created_at INTEGER NOT NULL, title TEXT NOT NULL DEFAULT '', columns TEXT NOT NULL DEFAULT '[]',
              request TEXT NOT NULL DEFAULT '', expired INTEGER NOT NULL DEFAULT 0, truncated_to INTEGER,
              retention_ms INTEGER NOT NULL);
            INSERT INTO conversations VALUES ('c', 1);
            INSERT INTO result_tables(name, conversation_id, turn_id, created_at, retention_ms)
              VALUES ('rt_20240101_000000', 'c', 1, 1, 100000000000);""")
    catalog = Catalog(app_db, clock=lambda: 5)
    assert catalog["rt_20240101_000000"].refreshable is False


def test_refreshing_a_table_leaves_the_system_prompt_byte_identical(build_agent, web_client):
    """The prompt lists the Conversation's tables in creation order; a Refresh must not reorder them, or the
    next turn misses the prompt cache."""
    client, agent, clock, _, (first, second) = ask(build_agent, web_client, RECENT, RECENT)
    cid = agent.tables[first].conversation_id
    before = agent.semantic_layer.system_prompt(agent.tables.tables_of(cid))
    clock.ms += HOUR
    assert refresh(client, first).status_code == 200
    assert [t.name for t in agent.tables.tables_of(cid)] == [first, second]
    assert agent.semantic_layer.system_prompt(agent.tables.tables_of(cid)) == before


def test_a_sweep_dropping_the_table_mid_refresh_gives_the_plain_expired_answer(build_agent, web_client, monkeypatch):
    client, agent, clock, _, (name,) = ask(build_agent, web_client, RECENT)
    normal = client.post(f"/api/results/{name}/refresh")  # baseline shape of an expired answer, below
    assert normal.status_code == 200
    real = queries.run_query

    def sweep_during(*args, **kwargs):
        result = real(*args, **kwargs)
        clock.ms += 2 * DAY
        agent.tables.sweep()  # the table's Retention is over: its rows are dropped
        return result

    monkeypatch.setattr(queries, "run_query", sweep_during)
    r = refresh(client, name)
    assert r.status_code == 410 and "expired" in r.json()["detail"].lower()
    assert result_tables_in_results_db(agent) == []  # no temporary table is left behind


def test_a_failed_swap_leaves_the_catalog_columns_as_they_were(build_agent, web_client, monkeypatch):
    client, agent, clock, _, (name,) = ask(build_agent, web_client, RECENT)
    before = agent.tables[name]
    clock.ms += HOUR

    def broken(*args, **kwargs):
        raise sqlite3.OperationalError("disk I/O error")

    monkeypatch.setattr(queries, "swap_table", broken)
    try:
        refresh(client, name)
    except sqlite3.OperationalError:
        pass
    after = agent.tables[name]
    assert (after.columns, after.as_of_ms, after.truncated_to) == (before.columns, before.as_of_ms, before.truncated_to)
    assert result_tables_in_results_db(agent) == [name]
