"""Refresh in notebooks: never a Run. AI Cell tables and Count Displays refresh, SQL Cell outputs have no
refresh control, and a refresh leaves every Run and the context sent to cells below unchanged."""

import re

from conftest import Clock, ScriptedLLM, add_ai_cell, add_sql_cell, latest_run, tool_call
from test_notebook_page import new_notebook, run_cell
from test_notebooks import SQL

from flood_agent.llm import TextChunk

RECENT_COUNT = "SELECT COUNT(*) FROM flows WHERE etime > now() - 1500"


def ai_count_cell(build_agent, web_client, monkeypatch):
    monkeypatch.setenv("FIXED_NOW_MS", "1700000003000")
    clock = Clock()
    llm = ScriptedLLM([tool_call("run_count_query", sql=RECENT_COUNT, title="Recent")], [TextChunk("Two.")])
    agent = build_agent(llm, clock=clock)
    client = web_client(agent)
    nb, cell = new_notebook(client)
    _, _, page = run_cell(client, nb, cell)
    return client, agent, clock, nb, cell, page


def test_an_ai_cells_count_display_refreshes_without_a_run_or_staleness(build_agent, web_client, monkeypatch):
    client, agent, clock, nb, cell, page = ai_count_cell(build_agent, web_client, monkeypatch)
    display_id = int(re.search(r'data-display-id="(\d+)"', page).group(1))
    below = add_ai_cell(agent, nb, prompt="and then?")
    before = latest_run(agent, nb, cell)

    monkeypatch.setenv("FIXED_NOW_MS", "1700000003600")
    clock.ms += 3_600_000
    r = client.post(f"/api/notebook-displays/{display_id}/refresh")

    assert r.status_code == 200 and r.json() == {"value": 1, "computed_at_ms": clock.ms}
    shown = client.get(f"/n/{nb}/cells/{cell}").text
    assert re.search(r'class="count-value"[^>]*>1<', shown)
    after = latest_run(agent, nb, cell)
    assert (after["id"], after["number"], after["messages"], after["context_fingerprint"]) == (
        before["id"], before["number"], before["messages"], before["context_fingerprint"])
    assert below not in agent.notebooks.stale_cells(nb)
    assert client.post("/api/notebook-displays/99999/refresh").status_code == 404


def test_refreshing_an_ai_cells_table_does_not_make_a_cell_below_stale_or_change_its_run(build_agent, web_client):
    llm = ScriptedLLM([tool_call("run_query", sql=SQL, title="t")], [TextChunk("Done.")], [TextChunk("Next.")])
    agent = build_agent(llm)
    client = web_client(agent)
    nb, cell = new_notebook(client)
    run_cell(client, nb, cell)
    second = add_ai_cell(agent, nb, prompt="then")
    run_cell(client, nb, second, prompt="then")
    run = latest_run(agent, nb, cell)
    [table] = agent.tables.tables_of_cell(cell)

    assert client.post(f"/api/results/{table.name}/refresh").status_code == 200

    assert agent.notebooks.stale_cells(nb) == set()
    again = latest_run(agent, nb, cell)
    assert (again["id"], again["number"], again["messages"]) == (run["id"], run["number"], run["messages"])
    assert latest_run(agent, nb, second)["number"] == 2  # no refresh took a run number


def sql_cell(client, agent, nb, sql):
    cell = add_sql_cell(agent, nb)
    client.post(f"/n/{nb}/cells/{cell}/run", data={"sql": sql, "max_rows": "10"})
    client.get(f"/n/{nb}/cells/{cell}/stream")
    return cell


def test_a_sql_cells_count_display_has_no_refresh_control(build_agent, web_client):
    agent = build_agent(ScriptedLLM())
    client = web_client(agent)
    nb, _ = new_notebook(client)
    cell = sql_cell(client, agent, nb, "SELECT COUNT(*) FROM flows")
    page = client.get(f"/n/{nb}/cells/{cell}").text
    count = page.split('class="count-display"')[1].split("</p>")[0]
    assert "<button" not in count and "data-display-id" not in page


def test_a_sql_cell_says_refreshed_when_its_table_was_refreshed_elsewhere(build_agent, web_client):
    clock = Clock()
    agent = build_agent(ScriptedLLM(), clock=clock)
    client = web_client(agent)
    nb, _ = new_notebook(client)
    cell = sql_cell(client, agent, nb, SQL)
    assert "refreshed" not in client.get(f"/n/{nb}/cells/{cell}").text.split('class="cell-foot sql-status"')[1]
    [table] = agent.tables.tables_of_cell(cell)

    clock.ms += 60_000
    assert client.post(f"/api/results/{table.name}/refresh").status_code == 200

    foot = client.get(f"/n/{nb}/cells/{cell}").text.split('class="cell-foot sql-status"')[1]
    assert "refreshed <time" in foot
    assert latest_run(agent, nb, cell)["number"] == 1


def test_the_earlier_tables_list_uses_the_generic_table_urls(build_agent, web_client):
    llm = ScriptedLLM([tool_call("run_query", sql=SQL, title="t")], [TextChunk("A.")], [TextChunk("B.")])
    agent = build_agent(llm)
    client = web_client(agent)
    nb, cell = new_notebook(client)
    run_cell(client, nb, cell)
    [table] = agent.tables.tables_of_cell(cell)
    _, _, page = run_cell(client, nb, cell)
    assert f"/api/results/{table.name}/settings" in page
