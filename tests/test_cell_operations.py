"""Cell operations: insert, move, copy and delete (with the keep or delete choice for `rt_` tables)."""

import asyncio

import pytest
from conftest import ScriptedLLM, tool_call
from test_notebooks import SQL, Slow, run

from flood_agent.agent import NotebookBusy
from flood_agent.llm import TextChunk


def ids(agent, nb):
    return [c["id"] for c in agent.notebooks.cells(nb)]


async def test_insert_above_the_first_cell_and_above_any_cell(make_agent):
    agent = await make_agent(ScriptedLLM())
    nb = agent.create_notebook()
    a = agent.add_ai_cell(nb, prompt="a")
    b = agent.add_ai_cell(nb, prompt="b")
    top = agent.add_markdown_cell(nb, text="top", before=a)
    mid = agent.add_sql_cell(nb, sql="SELECT 1", before=b)
    assert ids(agent, nb) == [top, a, mid, b]


async def test_move_swaps_a_cell_with_its_neighbour_and_the_ends_stay_put(make_agent):
    agent = await make_agent(ScriptedLLM())
    nb = agent.create_notebook()
    a, b, c = (agent.add_ai_cell(nb, prompt=p) for p in "abc")
    agent.move_cell(nb, c, "up")
    assert ids(agent, nb) == [a, c, b]
    agent.move_cell(nb, a, "down")
    assert ids(agent, nb) == [c, a, b]
    agent.move_cell(nb, c, "up")  # already first: nothing happens
    agent.move_cell(nb, b, "down")
    assert ids(agent, nb) == [c, a, b]


async def test_copying_an_ai_cell_copies_prompt_and_settings_but_no_run(make_agent):
    agent = await make_agent(ScriptedLLM([TextChunk("Answer.")]))
    nb = agent.create_notebook()
    a = agent.add_ai_cell(nb, prompt="ask", model="m1", effort="high", max_rows=50)
    after = agent.add_ai_cell(nb, prompt="after")
    await run(agent, nb, a)
    copy = agent.copy_cell(nb, a)
    assert ids(agent, nb) == [a, copy, after]
    cell = agent.notebooks.cell(copy)
    assert cell["draft"] == agent.notebooks.cell(a)["draft"] == {"prompt": "ask", "model": "m1", "effort": "high", "max_rows": 50}
    assert cell["run"] is None


async def test_copying_sql_and_markdown_cells_copies_their_drafts(make_agent):
    agent = await make_agent(ScriptedLLM())
    nb = agent.create_notebook()
    s = agent.add_sql_cell(nb, sql="SELECT 1", max_rows=7)
    m = agent.add_markdown_cell(nb, text="note", send_to_ai=True)
    assert agent.notebooks.cell(agent.copy_cell(nb, s))["draft"] == {"sql": "SELECT 1", "max_rows": 7, "command": "", "table": ""}
    assert agent.notebooks.cell(agent.copy_cell(nb, m))["draft"] == {"text": "note", "send_to_ai": True}


async def test_deleting_a_cell_removes_it_and_its_run(make_agent):
    agent = await make_agent(ScriptedLLM([TextChunk("Answer.")]))
    nb = agent.create_notebook()
    a, b = agent.add_ai_cell(nb, prompt="a"), agent.add_ai_cell(nb, prompt="b")
    await run(agent, nb, a)
    agent.delete_cell(nb, a)
    assert ids(agent, nb) == [b]
    with pytest.raises(KeyError):
        agent.notebooks.cell(a)


async def _cell_with_rt_and_saved(make_agent):
    llm = ScriptedLLM(
        [tool_call("run_query", sql=SQL, title="First", call_id="c1"),
         tool_call("run_query", sql="SELECT sip FROM flows", title="Kept", call_id="c2")], [TextChunk("one")],
        [tool_call("run_query", sql=SQL, title="Now")], [TextChunk("two")])
    agent = await make_agent(llm)
    nb = agent.create_notebook()
    a = agent.add_ai_cell(nb, prompt="a")
    await run(agent, nb, a)
    rt, keep = agent.tables.tables_of_run(a, agent.notebooks.cell(a)["run"]["id"])
    agent.tables.update(keep.name, new_name="kept_ports")
    await run(agent, nb, a)
    [current] = agent.tables.tables_of_run(a, agent.notebooks.cell(a)["run"]["id"])
    return agent, nb, a, rt, current


async def test_delete_with_the_delete_choice_drops_current_and_earlier_rt_tables_but_keeps_saved(make_agent):
    agent, nb, a, rt, current = await _cell_with_rt_and_saved(make_agent)
    assert agent.cell_delete_preview(nb, a) == {"result": 2, "saved": 1}
    agent.delete_cell(nb, a, "delete_rt")
    assert rt.name not in agent.tables and current.name not in agent.tables
    assert "kept_ports" in agent.tables


async def test_delete_with_the_keep_choice_leaves_the_tables_to_expire(make_agent):
    agent, nb, a, rt, current = await _cell_with_rt_and_saved(make_agent)
    agent.delete_cell(nb, a, "keep")
    assert rt.name in agent.tables and current.name in agent.tables and "kept_ports" in agent.tables


async def test_moving_or_deleting_a_cell_makes_the_cells_below_stale(make_agent):
    agent = await make_agent(ScriptedLLM([TextChunk("A.")], [TextChunk("B.")], [TextChunk("C.")]))
    nb = agent.create_notebook()
    a, b, c = (agent.add_ai_cell(nb, prompt=p) for p in "abc")
    for cell in (a, b, c):
        await run(agent, nb, cell)
    assert agent.stale_cells(nb) == set()
    agent.move_cell(nb, c, "up")  # now a, c, b: c no longer sees b, and b now sees c
    assert agent.stale_cells(nb) == {b, c}
    agent.move_cell(nb, c, "down")
    assert agent.stale_cells(nb) == set()
    agent.delete_cell(nb, a)
    assert agent.stale_cells(nb) == {b, c}


async def test_a_running_cell_cannot_be_moved_copied_or_deleted(make_agent):
    agent = await make_agent(Slow())
    nb = agent.create_notebook()
    a, b = agent.add_ai_cell(nb, prompt="a"), agent.add_ai_cell(nb, prompt="b")
    task = asyncio.create_task(run(agent, nb, a))
    while agent.running_cell(nb) != a:
        await asyncio.sleep(0.01)
    for op in (lambda: agent.move_cell(nb, a, "down"), lambda: agent.delete_cell(nb, a), lambda: agent.copy_cell(nb, a)):
        with pytest.raises(NotebookBusy):
            op()
    agent.stop_cell(a)
    await task
    assert ids(agent, nb) == [a, b]
