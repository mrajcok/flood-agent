"""Cell operations: insert, move, copy and delete (with the keep or delete choice for `rt_` tables)."""

import asyncio

import pytest
from conftest import ScriptedLLM, add_ai_cell, add_markdown_cell, add_sql_cell, tool_call
from test_notebooks import SQL, Slow, run

from flood_agent.llm import TextChunk
from flood_agent.notebook import NotebookBusy


def ids(agent, nb):
    return [c["id"] for c in agent.notebook_store.cells(nb)]


async def test_move_swaps_a_cell_with_its_neighbour_and_the_ends_stay_put(make_agent):
    agent = await make_agent(ScriptedLLM())
    nb = agent.notebooks.create_notebook()
    a, b, c = (add_ai_cell(agent, nb, prompt=p) for p in "abc")
    agent.notebooks.move_cell(nb, c, "up")
    assert ids(agent, nb) == [a, c, b]
    agent.notebooks.move_cell(nb, a, "down")
    assert ids(agent, nb) == [c, a, b]
    agent.notebooks.move_cell(nb, c, "up")  # already first: nothing happens
    agent.notebooks.move_cell(nb, b, "down")
    assert ids(agent, nb) == [c, a, b]


async def test_copying_an_ai_cell_copies_prompt_and_settings_but_no_run(make_agent):
    agent = await make_agent(ScriptedLLM([TextChunk("Answer.")]))
    nb = agent.notebooks.create_notebook()
    a = add_ai_cell(agent, nb, prompt="ask", model="m1", effort="high", max_rows=50)
    after = add_ai_cell(agent, nb, prompt="after")
    await run(agent, nb, a)
    copy = agent.notebooks.copy_cell(nb, a)
    assert ids(agent, nb) == [a, copy, after]
    cell = agent.notebook_store.cell(copy)
    assert cell["draft"] == agent.notebook_store.cell(a)["draft"] == {"prompt": "ask", "model": "m1", "effort": "high", "max_rows": 50}
    assert cell["run"] is None


async def test_copying_sql_and_markdown_cells_copies_their_drafts(make_agent):
    agent = await make_agent(ScriptedLLM())
    nb = agent.notebooks.create_notebook()
    s = add_sql_cell(agent, nb, sql="SELECT 1", max_rows=7)
    m = add_markdown_cell(agent, nb, text="note", send_to_ai=True)
    assert agent.notebook_store.cell(agent.notebooks.copy_cell(nb, s))["draft"] == {"sql": "SELECT 1", "max_rows": 7, "command": "", "table": ""}
    assert agent.notebook_store.cell(agent.notebooks.copy_cell(nb, m))["draft"] == {"text": "note", "send_to_ai": True, "displays": []}


async def test_deleting_a_cell_removes_it_and_its_run(make_agent):
    agent = await make_agent(ScriptedLLM([TextChunk("Answer.")]))
    nb = agent.notebooks.create_notebook()
    a, b = add_ai_cell(agent, nb, prompt="a"), add_ai_cell(agent, nb, prompt="b")
    await run(agent, nb, a)
    agent.notebooks.delete_cell(nb, a)
    assert ids(agent, nb) == [b]
    with pytest.raises(KeyError):
        agent.notebook_store.cell(a)


async def _cell_with_rt_and_saved(make_agent):
    llm = ScriptedLLM(
        [tool_call("run_query", sql=SQL, title="First", call_id="c1"),
         tool_call("run_query", sql="SELECT sip FROM flows", title="Kept", call_id="c2")], [TextChunk("one")],
        [tool_call("run_query", sql=SQL, title="Now")], [TextChunk("two")])
    agent = await make_agent(llm)
    nb = agent.notebooks.create_notebook()
    a = add_ai_cell(agent, nb, prompt="a")
    await run(agent, nb, a)
    rt, keep = agent.tables.tables_of_run(a, agent.notebook_store.cell(a)["run"]["id"])
    agent.tables.update(keep.name, new_name="kept_ports")
    await run(agent, nb, a)
    [current] = agent.tables.tables_of_run(a, agent.notebook_store.cell(a)["run"]["id"])
    return agent, nb, a, rt, current


async def test_delete_with_the_delete_choice_drops_current_and_earlier_rt_tables_but_keeps_saved(make_agent):
    agent, nb, a, rt, current = await _cell_with_rt_and_saved(make_agent)
    assert agent.notebooks.cell_delete_preview(nb, a) == {"result": 2, "saved": 1}
    agent.notebooks.delete_cell(nb, a, "delete_rt")
    assert rt.name not in agent.tables and current.name not in agent.tables
    assert "kept_ports" in agent.tables


async def test_delete_with_the_keep_choice_leaves_the_tables_to_expire(make_agent):
    agent, nb, a, rt, current = await _cell_with_rt_and_saved(make_agent)
    agent.notebooks.delete_cell(nb, a, "keep")
    assert rt.name in agent.tables and current.name in agent.tables and "kept_ports" in agent.tables


async def test_moving_or_deleting_a_cell_makes_the_cells_below_stale(make_agent):
    agent = await make_agent(ScriptedLLM([TextChunk("A.")], [TextChunk("B.")], [TextChunk("C.")]))
    nb = agent.notebooks.create_notebook()
    a, b, c = (add_ai_cell(agent, nb, prompt=p) for p in "abc")
    for cell in (a, b, c):
        await run(agent, nb, cell)
    assert agent.notebooks.stale_cells(nb) == set()
    agent.notebooks.move_cell(nb, c, "up")  # now a, c, b: c no longer sees b, and b now sees c
    assert agent.notebooks.stale_cells(nb) == {b, c}
    agent.notebooks.move_cell(nb, c, "down")
    assert agent.notebooks.stale_cells(nb) == set()
    agent.notebooks.delete_cell(nb, a)
    assert agent.notebooks.stale_cells(nb) == {b, c}


async def test_a_running_cell_cannot_be_moved_copied_deleted_or_cleared_of_earlier_tables(make_agent):
    agent = await make_agent(Slow())
    nb = agent.notebooks.create_notebook()
    a, b = add_ai_cell(agent, nb, prompt="a"), add_ai_cell(agent, nb, prompt="b")
    task = asyncio.create_task(run(agent, nb, a))
    while agent.notebooks.running_cell(nb) != a:
        await asyncio.sleep(0.01)
    for op in (lambda: agent.notebooks.move_cell(nb, a, "down"), lambda: agent.notebooks.delete_cell(nb, a), lambda: agent.notebooks.copy_cell(nb, a),
               lambda: agent.notebooks.delete_earlier_tables(nb, a)):
        with pytest.raises(NotebookBusy):
            op()
    agent.notebooks.stop_cell(nb, a)
    await task
    assert ids(agent, nb) == [a, b]
