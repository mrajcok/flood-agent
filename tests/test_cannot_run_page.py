"""The Run route saves the Draft, then refuses a Cell that cannot be Run with the Cell module's reason."""

import re

from conftest import ScriptedLLM
from test_notebook_page import new_notebook

from flood_agent import queries


def test_run_saves_the_draft_then_refuses_a_cell_that_cannot_run_with_its_reason(build_agent, web_client):
    agent = build_agent(ScriptedLLM())
    client = web_client(agent)
    nb, first = new_notebook(client)
    added = client.post(f"/n/{nb}/cells", data={"kind": "sql", "after": first})
    cell = re.search(r'data-cell-id="(\w+)"', added.text).group(1)
    client.post(f"/n/{nb}/cells/{cell}/run", data={"sql": "SELECT 1 AS one", "max_rows": "10"})
    client.get(f"/n/{nb}/cells/{cell}/stream")
    ran = agent.notebook_store.cell(cell)["run"]

    limited = "SELECT dport FROM flows LIMIT 5"
    refused = client.post(f"/n/{nb}/cells/{cell}/run", data={"sql": limited, "max_rows": "10"})

    assert refused.status_code == 422 and refused.json()["detail"] == queries.LIMIT_IN_SQL_CELL
    assert agent.notebooks.cell(nb, cell).draft.sql == limited  # kept as the Draft
    assert agent.notebook_store.cell(cell)["run"] == ran  # no Run was started
