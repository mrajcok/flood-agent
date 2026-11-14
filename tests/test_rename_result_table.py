"""Renaming a Result Table is a real SQL rename, at the HTTP seam."""


import pytest
from conftest import ScriptedLLM, last_tool_result, query_limits, register_table, tool_call
from conftest import ask as run_in_notebook

from flood_agent import queries
from flood_agent.llm import TextChunk
from flood_agent.web import TEMPLATES

BY_PORT = "SELECT dport, SUM(bytes) AS total_bytes FROM flows GROUP BY dport"


def chart_spec(t):
    return {"data": {"name": t}, "mark": "bar", "encoding": {
        "x": {"field": "dport", "type": "nominal"}, "y": {"field": "total_bytes", "type": "quantitative"}}}


def make(build_agent, web_client, *followups):
    """One AI Cell that makes a Result Table, shows it and charts it; then any scripted follow-up cells."""
    seen = {}

    def table(msgs):
        seen["t"] = last_tool_result(msgs)["result_table"]
        return [tool_call("show_table", result_table=seen["t"])]

    def chart(msgs):
        return [tool_call("show_chart", vega_lite_spec=chart_spec(seen["t"]))]

    llm = ScriptedLLM(
        [tool_call("run_query", sql=BY_PORT, title="Bytes per port")],
        table, chart, [TextChunk("here")], *followups)
    client = web_client(build_agent(llm))
    nb, _, _ = run_in_notebook(client, "ports")
    return client, nb, seen["t"]


def rename(client, old, new):
    return client.post(f"/api/results/{old}/settings", data={"name": new})


def test_rename_changes_the_name_everywhere_and_keeps_displays_loading(build_agent, web_client):
    client, nb, old = make(build_agent, web_client)
    resp = rename(client, old, "  top_ports ")
    assert resp.status_code == 200 and resp.json()["name"] == "top_ports"
    assert client.get("/api/results/top_ports/rows").json()["name"] == "top_ports"
    assert client.get(f"/api/results/{old}/rows").status_code == 404
    assert client.get("/api/results/top_ports/chart-rows").json()[0]["dport"] == 8443
    assert client.get("/api/results/top_ports/psv").status_code == 200
    page = client.get(f"/n/{nb}").text
    assert "/api/results/top_ports/rows" in page and "/api/results/top_ports/chart-rows" in page


def test_new_name_is_queryable_and_old_is_not(build_agent, web_client):
    results, seen = [], {}

    def count(table):
        def step(msgs):
            if table is None:  # the second query follows the first one's result
                results.append(last_tool_result(msgs))
            return [tool_call("run_count_query", sql=f"SELECT COUNT(*) FROM {seen.get('old') if table is None else table}", title="T")]
        return step

    def done(msgs):
        results.append(last_tool_result(msgs))
        return [TextChunk("done")]

    client, nb, seen["old"] = make(build_agent, web_client, count("top_ports"), count(None), done)
    assert rename(client, seen["old"], "top_ports").status_code == 200
    run_in_notebook(client, "again", nb)
    assert results[-2]["success"] is True and results[-2]["count"] > 0
    assert results[-1]["success"] is False


@pytest.mark.parametrize("bad", ["1abc", "has space", "a-b", "x" * 61, "rt_abc", "lookup_ips", "drop;table", "é"])
def test_invalid_or_taken_names_are_rejected_and_nothing_changes(build_agent, web_client, bad):
    client, _, old = make(build_agent, web_client)
    resp = rename(client, old, bad)
    assert resp.status_code == 422 and resp.json()["detail"]
    assert client.get(f"/api/results/{old}/rows").status_code == 200


def test_saving_the_unchanged_name_with_a_new_retention_changes_only_the_retention(build_agent, web_client):
    client, _, old = make(build_agent, web_client)
    resp = client.post(f"/api/results/{old}/settings", data={"name": old, "retention_days": "7"})
    assert resp.status_code == 200 and resp.json()["name"] == old and resp.json()["retention_days"] == 7
    assert client.get(f"/api/results/{old}/rows").json()["retention_days"] == 7


def two_tables(build_agent, web_client):
    agent = build_agent(ScriptedLLM())
    client = web_client(agent)
    names = []
    for _ in range(2):
        meta = queries.run_query(agent.config.telemetry_db, agent.config.results_db, BY_PORT, **query_limits())
        register_table(agent.tables, meta["result_table"])
        names.append(meta["result_table"])
    return agent, client, names


def test_renaming_to_another_result_tables_name_is_rejected_ignoring_case(build_agent, web_client):
    _, client, (a, b) = two_tables(build_agent, web_client)
    assert rename(client, a, "Mine").status_code == 200
    resp = rename(client, b, "mine")
    assert resp.status_code == 422 and "already exists" in resp.json()["detail"]
    assert client.get(f"/api/results/{b}/rows").status_code == 200


def test_a_longer_retention_survives_a_rename(build_agent, web_client):
    client, _, old = make(build_agent, web_client)
    client.post(f"/api/results/{old}/settings", data={"retention_days": "90"})
    assert rename(client, old, "kept").json()["retention_days"] == 90
    assert client.get("/api/results/kept/rows").json()["retention_days"] == 90


def test_renaming_without_a_retention_keeps_the_table_30_days(build_agent, web_client):
    client, _, old = make(build_agent, web_client)
    assert rename(client, old, "kept").json()["retention_days"] == 30


def test_a_retention_given_with_the_rename_wins(build_agent, web_client):
    client, _, old = make(build_agent, web_client)
    resp = client.post(f"/api/results/{old}/settings", data={"name": "kept", "retention_days": "2"})
    assert resp.json()["retention_days"] == 2


@pytest.mark.parametrize("days", ["0", "-3", "3651", "nan", "soon"])
def test_a_bad_retention_is_refused_with_nothing_changed(build_agent, web_client, days):
    client, _, old = make(build_agent, web_client)
    resp = client.post(f"/api/results/{old}/settings", data={"name": "kept", "retention_days": days})
    assert resp.status_code == 422
    assert client.get(f"/api/results/{old}/rows").json()["retention_days"] == 1


def test_a_name_typed_with_capitals_is_stored_in_lower_case(build_agent, web_client):
    client, _, old = make(build_agent, web_client)
    assert rename(client, old, "MyTable").json()["name"] == "mytable"
    assert client.get("/api/results/MyTable/rows").json()["name"] == "mytable"


def test_deleting_the_notebook_with_its_result_tables_keeps_the_saved_table(build_agent, web_client):
    client, nb, old = make(build_agent, web_client)
    rename(client, old, "kept")
    client.post(f"/n/{nb}/delete", data={"tables": "delete_rt"})
    assert client.get("/api/results/kept/rows").status_code == 200


def test_display_shows_the_sql_name_with_a_copy_icon_that_inserts_it_into_the_chat():
    html = TEMPLATES.get_template("display_table.html").render(
        rows_url="/r", psv_url="/p", result_table="t", columns=[], settings_url="/s")
    assert 'class="copy"' in html and "insertIntoPrompt(name)" in html
