"""Explain a Generated SQL from a Trace: POST /api/explain returns its Query Plan, computed without the LLM
and under the same safety rails as a real query (HTTP seam)."""

from test_refresh_table import ask

SUBQUERY = "SELECT COUNT(*) FROM flows WHERE dport IN (SELECT dport FROM flows LIMIT 1)"


def explain(client, sql):
    return client.post("/api/explain", data={"sql": sql})


def test_the_plan_is_a_tree_of_steps(build_agent, web_client):
    client, *_ = ask(build_agent, web_client, "SELECT 1 AS one")
    r = explain(client, SUBQUERY)
    assert r.status_code == 200
    assert r.json()["plan"] == [
        {"depth": 0, "detail": "SCAN flows"},
        {"depth": 0, "detail": "LIST SUBQUERY 1"},
        {"depth": 1, "detail": "SCAN flows"},
    ]


def test_an_index_shows_in_the_plan(build_agent, web_client):
    client, *_ = ask(build_agent, web_client, "SELECT 1 AS one")
    plan = explain(client, "SELECT dport FROM flows WHERE etime > 5").json()["plan"]
    assert plan == [{"depth": 0, "detail": "SEARCH flows USING INDEX flows_etime (etime>?)"}]


def test_any_live_result_table_can_be_explained_and_nothing_is_created(build_agent, web_client):
    client, agent, *_, (name,) = ask(build_agent, web_client, "SELECT dport FROM flows")
    r = explain(client, f"SELECT * FROM {name};")
    assert r.status_code == 200
    assert r.json()["plan"] == [{"depth": 0, "detail": f"SCAN {name}"}]
    assert agent.tables.names() == [name]


def test_anything_but_a_single_select_is_refused(build_agent, web_client):
    client, *_ = ask(build_agent, web_client, "SELECT 1 AS one")
    r = explain(client, "DROP TABLE flows")
    assert r.status_code == 422
    assert "Only a single read-only SELECT" in r.json()["detail"]


def test_a_table_that_is_gone_shows_the_raw_error(build_agent, web_client):
    client, *_ = ask(build_agent, web_client, "SELECT 1 AS one")
    r = explain(client, "SELECT * FROM rt_20200101_000000")
    assert r.status_code == 422
    assert r.json()["detail"] == "no such table: rt_20200101_000000"
