"""Stale AI Cells: an AI Cell is stale when the context it would be sent now differs from the one its Run was sent."""

from dataclasses import replace

from conftest import ScriptedLLM, add_ai_cell, add_markdown_cell, latest_run, tool_call

from flood_agent.llm import TextChunk

SQL = "SELECT dport, SUM(bytes) AS total_bytes FROM flows GROUP BY dport ORDER BY total_bytes DESC"


async def run(agent, nb, cell):
    return [ev async for ev in agent.notebooks.run_cell(nb, cell)]


async def two_ai_cells(make_agent, *steps):
    agent = await make_agent(ScriptedLLM(*steps))
    nb = agent.notebooks.create_notebook()
    above = add_ai_cell(agent, nb, prompt="first")
    below = add_ai_cell(agent, nb, prompt="second")
    await run(agent, nb, above)
    await run(agent, nb, below)
    return agent, nb, above, below


async def test_rerunning_an_edited_cell_above_makes_the_cell_below_stale_until_it_is_rerun(make_agent):
    agent, nb, above, below = await two_ai_cells(
        make_agent, [TextChunk("A.")], [TextChunk("B.")], [TextChunk("A2.")], [TextChunk("B2.")])
    agent.notebooks.save_draft(nb, above, replace(agent.notebooks.cell(nb, above).draft, prompt="first, edited"))
    assert agent.notebooks.stale_ids(nb) == []  # edited but not run again: cells below are sent what ran
    await run(agent, nb, above)
    assert agent.notebooks.stale_ids(nb) == [below]  # the cell above is not stale of itself
    await run(agent, nb, below)
    assert agent.notebooks.stale_ids(nb) == []


async def test_refreshing_or_renaming_a_table_above_never_makes_a_cell_stale(make_agent):
    agent = await make_agent(ScriptedLLM(
        [tool_call("run_query", sql=SQL, title="Ports")], [TextChunk("A.")], [TextChunk("B.")]))
    nb = agent.notebooks.create_notebook()
    above = add_ai_cell(agent, nb, prompt="first")
    below = add_ai_cell(agent, nb, prompt="second")
    await run(agent, nb, above)
    await run(agent, nb, below)
    [table] = agent.tables.tables_of_run(above, latest_run(agent, nb, above)["id"])
    agent.refresh_table(table.name)
    assert agent.notebooks.stale_ids(nb) == []
    agent.tables.update(table.name, new_name="renamed_ports")
    assert agent.notebooks.stale_ids(nb) == []


def test_the_page_marks_a_stale_cell_and_serves_the_stale_cells(build_agent, web_client):
    from test_notebook_page import new_notebook, run_cell

    agent = build_agent(ScriptedLLM([TextChunk("A.")], [TextChunk("B.")]))
    client = web_client(agent)
    nb, first = new_notebook(client)
    run_cell(client, nb, first, prompt="first")
    second = add_ai_cell(agent, nb, prompt="second")
    run_cell(client, nb, second, prompt="second")
    assert client.get(f"/n/{nb}/stale").json() == []
    assert 'data-stale="true"' not in client.get(f"/n/{nb}").text

    add_markdown_cell(agent, nb, "Focus on UDP.", send_to_ai=True, after=first)
    assert client.get(f"/n/{nb}/stale").json() == [second]
    assert client.get(f"/n/{nb}").text.count('data-stale="true"') == 1
    assert 'data-stale="true"' in client.get(f"/n/{nb}/cells/{second}").text
    assert client.get("/n/nope/stale").status_code == 404
