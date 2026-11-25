"""Renaming a Result Table through the settings route: Displays and queries follow the new name. The naming and
Retention rules are tested on the Table Catalog (test_catalog_rename.py)."""


from conftest import ScriptedLLM, last_tool_result, tool_call
from conftest import ask as run_in_notebook

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


def test_display_shows_the_sql_name_with_a_copy_icon_that_inserts_it_into_the_chat():
    html = TEMPLATES.get_template("display_table.html").render(
        rows_url="/r", psv_url="/p", result_table="t", columns=[], settings_url="/s")
    assert 'class="copy"' in html and "insertIntoPrompt(name)" in html
