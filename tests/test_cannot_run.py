"""A Cell that cannot be Run is refused by the Agent before a Run starts: no Run is recorded and the latest Run and
its output stay."""

from dataclasses import replace

import pytest
from conftest import ScriptedLLM, add_sql_cell, latest_run

from flood_agent.cell import CannotRun


async def run(agent, notebook_id, cell_id):
    return [ev async for ev in agent.notebooks.run_cell(notebook_id, cell_id)]


async def test_a_sql_cell_that_cannot_run_is_refused_and_its_latest_run_stays(build_agent):
    agent = build_agent(ScriptedLLM())  # never started: a SQL Cell needs no MCP server
    nb = agent.notebooks.create_notebook()
    cell = add_sql_cell(agent, nb, sql="SELECT 1 AS one")
    await run(agent, nb, cell)
    before = latest_run(agent, nb, cell)

    for sql in ("SELECT dport FROM flows LIMIT 5", "SHOW TABLES", " "):
        agent.notebooks.save_draft(nb, cell, replace(agent.notebooks.cell(nb, cell).draft, sql=sql))
        with pytest.raises(CannotRun):
            await run(agent, nb, cell)
        assert latest_run(agent, nb, cell) == before, sql
    assert agent.notebooks.running_cell(nb) is None
