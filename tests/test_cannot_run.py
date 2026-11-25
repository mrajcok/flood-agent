"""A Cell that cannot be Run is refused by the Agent before a Run starts: no Run is recorded and the latest Run and
its output stay."""

from dataclasses import replace

import pytest
from conftest import ScriptedLLM, add_ai_cell, add_markdown_cell, add_sql_cell, latest_run

from flood_agent.cell import CannotRun
from flood_agent.llm import TextChunk


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


async def test_a_sql_cell_ending_in_limit_is_refused_with_a_pointer_to_the_row_cap(build_agent):
    agent = build_agent(ScriptedLLM())
    nb = agent.notebooks.create_notebook()
    cell = add_sql_cell(agent, nb, sql="SELECT dport FROM flows LIMIT 5")

    with pytest.raises(CannotRun, match="row cap"):
        await run(agent, nb, cell)
    assert latest_run(agent, nb, cell) is None


async def test_an_ai_cell_with_an_empty_prompt_is_refused_and_the_llm_is_not_called(make_agent):
    llm = ScriptedLLM([TextChunk("Hello.")])
    agent = await make_agent(llm)
    nb = agent.notebooks.create_notebook()
    cell = add_ai_cell(agent, nb, prompt="say hello")
    await run(agent, nb, cell)
    before = latest_run(agent, nb, cell)

    agent.notebooks.save_draft(nb, cell, replace(agent.notebooks.cell(nb, cell).draft, prompt="  \n"))
    with pytest.raises(CannotRun, match="Write a prompt to run"):
        await run(agent, nb, cell)
    assert latest_run(agent, nb, cell) == before
    assert len(llm.calls) == 1  # only the first Run's
    assert agent.notebooks.running_cell(nb) is None


async def test_a_markdown_cell_is_refused(build_agent):
    agent = build_agent(ScriptedLLM())
    nb = agent.notebooks.create_notebook()
    cell = add_markdown_cell(agent, nb, "# Notes")

    with pytest.raises(CannotRun, match="never Run"):
        await run(agent, nb, cell)
