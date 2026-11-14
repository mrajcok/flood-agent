"""Stale AI Cells: an AI Cell is stale when the context it would be sent now differs from the one its Run was sent."""

from conftest import ScriptedLLM, tool_call

from flood_agent.llm import TextChunk

SQL = "SELECT dport, SUM(bytes) AS total_bytes FROM flows GROUP BY dport ORDER BY total_bytes DESC"


async def run(agent, nb, cell):
    return [ev async for ev in agent.run_cell(nb, cell)]


async def two_ai_cells(make_agent, *steps):
    agent = await make_agent(ScriptedLLM(*steps))
    nb = agent.create_notebook()
    above = agent.add_ai_cell(nb, prompt="first")
    below = agent.add_ai_cell(nb, prompt="second")
    await run(agent, nb, above)
    await run(agent, nb, below)
    return agent, nb, above, below


async def test_a_cell_that_just_ran_is_not_stale_and_a_never_run_cell_is_not_either(make_agent):
    agent, nb, above, below = await two_ai_cells(make_agent, [TextChunk("A.")], [TextChunk("B.")])
    spare = agent.add_ai_cell(nb, prompt="never run")
    assert agent.stale_cells(nb) == set()
    assert spare not in agent.stale_cells(nb)


async def test_rerunning_an_edited_cell_above_makes_the_cell_below_stale_until_it_is_rerun(make_agent):
    agent, nb, above, below = await two_ai_cells(
        make_agent, [TextChunk("A.")], [TextChunk("B.")], [TextChunk("A2.")], [TextChunk("B2.")])
    agent.notebooks.edit_ai_cell(above, prompt="first, edited")
    assert agent.stale_cells(nb) == set()  # edited but not re-run: cells below are sent what ran
    await run(agent, nb, above)
    assert agent.stale_cells(nb) == {below}  # the cell above is not stale of itself
    await run(agent, nb, below)
    assert agent.stale_cells(nb) == set()


async def test_inserting_a_ticked_note_above_marks_stale_but_an_unticked_or_empty_one_does_not(make_agent):
    agent, nb, above, below = await two_ai_cells(make_agent, [TextChunk("A.")], [TextChunk("B.")])
    agent.add_markdown_cell(nb, "scratch", send_to_ai=False, after=above)
    agent.add_markdown_cell(nb, "   ", send_to_ai=True, after=above)
    assert agent.stale_cells(nb) == set()
    agent.add_markdown_cell(nb, "Focus on UDP.", send_to_ai=True, after=above)
    assert agent.stale_cells(nb) == {below}


async def test_toggling_send_to_ai_above_marks_stale_and_toggling_back_clears_it(make_agent):
    agent = await make_agent(ScriptedLLM([TextChunk("B.")]))
    nb = agent.create_notebook()
    note = agent.add_markdown_cell(nb, "note", send_to_ai=True)
    cell = agent.add_ai_cell(nb, prompt="ask")
    await run(agent, nb, cell)
    agent.notebooks.edit_ai_cell(note, send_to_ai=False)
    assert agent.stale_cells(nb) == {cell}
    agent.notebooks.edit_ai_cell(note, send_to_ai=True)
    assert agent.stale_cells(nb) == set()


async def test_an_sql_cell_is_never_stale_but_rerunning_it_makes_the_ai_cell_below_stale(make_agent):
    agent = await make_agent(ScriptedLLM([TextChunk("B.")]))
    nb = agent.create_notebook()
    sql = agent.add_sql_cell(nb, sql=SQL)
    note = agent.add_markdown_cell(nb, "note", send_to_ai=True, after=sql)
    ai = agent.add_ai_cell(nb, prompt="ask", after=note)
    await run(agent, nb, sql)
    await run(agent, nb, ai)
    agent.notebooks.edit_ai_cell(note, send_to_ai=False)
    assert agent.stale_cells(nb) == {ai}  # the SQL Cell above it is not listed
    agent.notebooks.edit_ai_cell(note, send_to_ai=True)
    agent.notebooks.edit_cell(sql, sql="SELECT dport FROM flows LIMIT 1")
    assert agent.stale_cells(nb) == set()  # a Draft alone changes nothing
    await run(agent, nb, sql)
    assert agent.stale_cells(nb) == {ai}


async def test_refreshing_or_renaming_a_table_above_never_makes_a_cell_stale(make_agent):
    agent = await make_agent(ScriptedLLM(
        [tool_call("run_query", sql=SQL, title="Ports")], [TextChunk("A.")], [TextChunk("B.")]))
    nb = agent.create_notebook()
    above = agent.add_ai_cell(nb, prompt="first")
    below = agent.add_ai_cell(nb, prompt="second")
    await run(agent, nb, above)
    await run(agent, nb, below)
    [table] = agent.tables.tables_of_run(above, agent.notebooks.cell(above)["run"]["id"])
    agent.refresh_table(table.name)
    assert agent.stale_cells(nb) == set()
    agent.tables.update(table.name, new_name="renamed_ports")
    assert agent.stale_cells(nb) == set()


def test_the_page_marks_a_stale_cell_and_serves_the_stale_cells(build_agent, web_client):
    from test_notebook_page import new_notebook, run_cell

    agent = build_agent(ScriptedLLM([TextChunk("A.")], [TextChunk("B.")]))
    client = web_client(agent)
    nb, first = new_notebook(client)
    run_cell(client, nb, first, prompt="first")
    second = agent.add_ai_cell(nb, prompt="second")
    run_cell(client, nb, second, prompt="second")
    assert client.get(f"/n/{nb}/stale").json() == []
    assert 'data-stale="true"' not in client.get(f"/n/{nb}").text

    agent.add_markdown_cell(nb, "Focus on UDP.", send_to_ai=True, after=first)
    assert client.get(f"/n/{nb}/stale").json() == [second]
    assert client.get(f"/n/{nb}").text.count('data-stale="true"') == 1
    assert 'data-stale="true"' in client.get(f"/n/{nb}/cells/{second}").text
    assert client.get("/n/nope/stale").status_code == 404
