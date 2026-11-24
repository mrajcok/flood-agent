"""The Notebook page's endpoints for SQL Cells: add, run, stream, show, Draft."""

import re

from conftest import ScriptedLLM
from test_notebook_page import new_notebook


def test_a_sql_cell_is_added_run_and_shown_without_the_llm(build_agent, web_client):
    llm = ScriptedLLM()
    client = web_client(build_agent(llm))
    nb, first = new_notebook(client)
    added = client.post(f"/n/{nb}/cells", data={"kind": "sql", "after": first})
    assert added.status_code == 200 and 'data-kind="sql"' in added.text
    cell = re.search(r'data-cell-id="(\w+)"', added.text).group(1)

    assert client.post(f"/n/{nb}/cells/{cell}/run", data={"sql": " ", "max_rows": "10"}).status_code == 422
    sql = "SELECT dport FROM flows GROUP BY dport"
    started = client.post(f"/n/{nb}/cells/{cell}/run", data={"sql": sql, "max_rows": "10"})
    assert started.status_code == 200 and f"/cells/{cell}/stream" in started.text
    assert "event: end" in client.get(f"/n/{nb}/cells/{cell}/stream").text
    page = client.get(f"/n/{nb}/cells/{cell}").text
    assert re.search(r"\d+ rows? &middot; [\d.]+ s &middot; ran", page) and "/api/results/rt_" in page
    assert "Only metadata about these results" in page and "AI Cells below" in page
    assert llm.calls == []

    bad = "SELECT nope FROM flows"
    assert client.post(f"/n/{nb}/cells/{cell}/draft", data={"sql": bad, "max_rows": "10"}).status_code == 204
    assert bad in client.get(f"/n/{nb}").text  # the Draft survives a reload
    client.post(f"/n/{nb}/cells/{cell}/run", data={"sql": bad, "max_rows": "10"})
    client.get(f"/n/{nb}/cells/{cell}/stream")
    failed = client.get(f"/n/{nb}/cells/{cell}").text
    assert "no such column: nope" in failed and "Only metadata about these results" not in failed


def test_the_edited_not_run_marker_on_a_sql_cell_is_the_typed_cells_answer(build_agent, web_client):
    """The rule's cases are in test_cell.py; here, only that the page shows what the typed Cell answers."""
    agent = build_agent(ScriptedLLM())
    client = web_client(agent)
    nb, first = new_notebook(client)
    added = client.post(f"/n/{nb}/cells", data={"kind": "sql", "after": first})
    cell = re.search(r'data-cell-id="(\w+)"', added.text).group(1)

    def edited() -> bool:
        return 'data-edited="true"' in client.get(f"/n/{nb}/cells/{cell}").text

    sql = "SELECT dport FROM flows"
    client.post(f"/n/{nb}/cells/{cell}/run", data={"sql": sql, "max_rows": "10"})
    client.get(f"/n/{nb}/cells/{cell}/stream")
    assert not agent.cell(cell).edited and not edited()
    client.post(f"/n/{nb}/cells/{cell}/draft", data={"sql": "SELECT sip FROM flows", "max_rows": "10"})
    page = client.get(f"/n/{nb}/cells/{cell}").text
    assert agent.cell(cell).edited and 'data-edited="true"' in page and "edited, not run" in page
