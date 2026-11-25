"""The Notebook page for drafts and re-runs: the "edited, not run" badge and tables from earlier runs."""

import re

from conftest import ScriptedLLM, latest_run, tool_call
from test_notebook_page import SONNET, new_notebook, run_cell
from test_notebooks import SQL

from flood_agent.llm import TextChunk


def edited(page: str) -> bool:
    return 'data-edited="true"' in page


def test_the_edited_not_run_marker_on_an_ai_cell_is_the_typed_cells_answer(build_agent, web_client):
    """The rule's cases are in test_cell.py; here, only that the page shows what the typed Cell answers."""
    agent = build_agent(ScriptedLLM([TextChunk("Old answer.")]))
    client = web_client(agent)
    nb, cell = new_notebook(client)
    run_cell(client, nb, cell, prompt="original", effort="high", max_rows="1000")
    assert not agent.notebooks.cell(nb, cell).edited and not edited(client.get(f"/n/{nb}/cells/{cell}").text)

    client.post(f"/n/{nb}/cells/{cell}/draft", data={"prompt": "changed", "model": SONNET, "effort": "high",
                                                     "max_rows": "1000"})
    page = client.get(f"/n/{nb}/cells/{cell}").text
    assert agent.notebooks.cell(nb, cell).edited and edited(page) and "edited, not run" in page and "Old answer." in page


def test_earlier_runs_tables_are_listed_collapsed_and_clearing_them_spares_saved_tables(build_agent, web_client):
    agent = build_agent(ScriptedLLM(
        [tool_call("run_query", sql=SQL, title="First", call_id="c1"),
         tool_call("run_query", sql="SELECT sip FROM flows", title="Keep", call_id="c2")], [TextChunk("one")],
        [tool_call("run_query", sql=SQL, title="Now")], [TextChunk("two")]))
    client = web_client(agent)
    nb, cell = new_notebook(client)
    run_cell(client, nb, cell)
    assert "Tables from earlier runs" not in client.get(f"/n/{nb}/cells/{cell}").text
    first_run = latest_run(agent, nb, cell)["id"]
    rt, keep = agent.tables.tables_of_run(cell, first_run)
    agent.tables.update(keep.name, new_name="kept_ports")
    run_cell(client, nb, cell)
    current = agent.tables.tables_of_run(cell, latest_run(agent, nb, cell)["id"])[0].name

    page = client.get(f"/n/{nb}/cells/{cell}").text
    assert "Tables from earlier runs (2)" in page
    section = page.split("Tables from earlier runs")[1]
    assert f"/api/results/{rt.name}/settings" in section and "/api/results/kept_ports/settings" in section
    assert f"/api/results/{current}/settings" not in section
    assert re.search(r'<details class="earlier-tables">', page)  # collapsed: no `open`
    assert "Delete all <code>rt_</code> tables" in page

    cleared = client.post(f"/n/{nb}/cells/{cell}/earlier-tables/delete")
    assert cleared.status_code == 200
    assert "Tables from earlier runs (1)" in cleared.text and "kept_ports" in cleared.text
    assert rt.name not in agent.tables and "kept_ports" in agent.tables and current in agent.tables


def test_clearing_earlier_tables_of_an_unknown_cell_is_404(build_agent, web_client):
    client = web_client(build_agent(ScriptedLLM()))
    nb, _ = new_notebook(client)
    assert client.post(f"/n/{nb}/cells/nope/earlier-tables/delete").status_code == 404
