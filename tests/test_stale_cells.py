"""Stale AI Cells: an AI Cell is stale when the context it would be sent now differs from the one its Run was sent."""

from dataclasses import replace

from conftest import ScriptedLLM, add_ai_cell, add_markdown_cell, add_sql_cell, tool_call

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


async def test_a_cell_that_just_ran_is_not_stale_and_a_never_run_cell_is_not_either(make_agent):
    agent, nb, above, below = await two_ai_cells(make_agent, [TextChunk("A.")], [TextChunk("B.")])
    spare = add_ai_cell(agent, nb, prompt="never run")
    assert agent.notebooks.stale_cells(nb) == set()
    assert spare not in agent.notebooks.stale_cells(nb)


async def test_rerunning_an_edited_cell_above_makes_the_cell_below_stale_until_it_is_rerun(make_agent):
    agent, nb, above, below = await two_ai_cells(
        make_agent, [TextChunk("A.")], [TextChunk("B.")], [TextChunk("A2.")], [TextChunk("B2.")])
    agent.notebooks.save_draft(nb, above, replace(agent.notebooks.cell(nb, above).draft, prompt="first, edited"))
    assert agent.notebooks.stale_cells(nb) == set()  # edited but not re-run: cells below are sent what ran
    await run(agent, nb, above)
    assert agent.notebooks.stale_cells(nb) == {below}  # the cell above is not stale of itself
    await run(agent, nb, below)
    assert agent.notebooks.stale_cells(nb) == set()


async def test_inserting_a_ticked_note_above_marks_stale_but_an_unticked_or_empty_one_does_not(make_agent):
    agent, nb, above, below = await two_ai_cells(make_agent, [TextChunk("A.")], [TextChunk("B.")])
    add_markdown_cell(agent, nb, "scratch", send_to_ai=False, after=above)
    add_markdown_cell(agent, nb, "   ", send_to_ai=True, after=above)
    assert agent.notebooks.stale_cells(nb) == set()
    add_markdown_cell(agent, nb, "Focus on UDP.", send_to_ai=True, after=above)
    assert agent.notebooks.stale_cells(nb) == {below}


async def test_toggling_send_to_ai_above_marks_stale_and_toggling_back_clears_it(make_agent):
    agent = await make_agent(ScriptedLLM([TextChunk("B.")]))
    nb = agent.notebooks.create_notebook()
    note = add_markdown_cell(agent, nb, "note", send_to_ai=True)
    cell = add_ai_cell(agent, nb, prompt="ask")
    await run(agent, nb, cell)
    agent.notebooks.save_draft(nb, note, replace(agent.notebooks.cell(nb, note).draft, send_to_ai=False))
    assert agent.notebooks.stale_cells(nb) == {cell}
    agent.notebooks.save_draft(nb, note, replace(agent.notebooks.cell(nb, note).draft, send_to_ai=True))
    assert agent.notebooks.stale_cells(nb) == set()


async def test_an_sql_cell_is_never_stale_but_rerunning_it_makes_the_ai_cell_below_stale(make_agent):
    agent = await make_agent(ScriptedLLM([TextChunk("B.")]))
    nb = agent.notebooks.create_notebook()
    sql = add_sql_cell(agent, nb, sql=SQL)
    note = add_markdown_cell(agent, nb, "note", send_to_ai=True, after=sql)
    ai = add_ai_cell(agent, nb, prompt="ask", after=note)
    await run(agent, nb, sql)
    await run(agent, nb, ai)
    agent.notebooks.save_draft(nb, note, replace(agent.notebooks.cell(nb, note).draft, send_to_ai=False))
    assert agent.notebooks.stale_cells(nb) == {ai}  # the SQL Cell above it is not listed
    agent.notebooks.save_draft(nb, note, replace(agent.notebooks.cell(nb, note).draft, send_to_ai=True))
    agent.notebooks.save_draft(nb, sql, replace(agent.notebooks.cell(nb, sql).draft, sql="SELECT DISTINCT dport FROM flows"))
    assert agent.notebooks.stale_cells(nb) == set()  # a Draft alone changes nothing
    await run(agent, nb, sql)
    assert agent.notebooks.stale_cells(nb) == {ai}


async def test_refreshing_or_renaming_a_table_above_never_makes_a_cell_stale(make_agent):
    agent = await make_agent(ScriptedLLM(
        [tool_call("run_query", sql=SQL, title="Ports")], [TextChunk("A.")], [TextChunk("B.")]))
    nb = agent.notebooks.create_notebook()
    above = add_ai_cell(agent, nb, prompt="first")
    below = add_ai_cell(agent, nb, prompt="second")
    await run(agent, nb, above)
    await run(agent, nb, below)
    [table] = agent.tables.tables_of_run(above, agent.notebook_store.cell(above)["run"]["id"])
    agent.refresh_table(table.name)
    assert agent.notebooks.stale_cells(nb) == set()
    agent.tables.update(table.name, new_name="renamed_ports")
    assert agent.notebooks.stale_cells(nb) == set()


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
