"""The Notebook module with AI Cells: storage, running a cell, stopping it."""

import asyncio
from dataclasses import replace

import pytest
from conftest import ScriptedLLM, add_ai_cell, tool_call

from flood_agent.catalog import CellRun
from flood_agent.events import TextDelta, TraceReady
from flood_agent.llm import TextChunk
from flood_agent.notebook_store import NotebookStore

SQL = "SELECT dport, SUM(bytes) AS total_bytes FROM flows GROUP BY dport ORDER BY total_bytes DESC"


async def run(agent, notebook_id, cell_id):
    return [ev async for ev in agent.notebooks.run_cell(notebook_id, cell_id)]


def run_of(agent, notebook_id, index):
    return agent.notebooks.notebook_page(notebook_id).cells[index].run


class Slow:
    """Streams some text, then waits for ever, like a slow model."""

    async def stream_chat(self, messages, tools, reasoning=None, session_id=None):
        yield TextChunk("Partial answer")
        await asyncio.Event().wait()


async def test_a_cell_sends_the_ai_cells_above_in_order_then_its_prompt_never_those_below(make_agent):
    llm = ScriptedLLM([TextChunk("Answer A.")], [TextChunk("Answer B.")], [TextChunk("Answer C.")])
    agent = await make_agent(llm)
    nb = agent.notebooks.create_notebook()
    a, b, c = (add_ai_cell(agent, nb, prompt=f"ask {p}") for p in "ABC")
    for cell in (a, b, c):
        await run(agent, nb, cell)
    llm.steps.append([TextChunk("Answer B2.")])
    await run(agent, nb, b)  # again: it must not see C, which is below it

    first, second, third, again = llm.calls
    assert [m["role"] for m in first] == ["system", "user"]
    assert [m["content"] for m in second[1:]] == ["ask A", "Answer A.", "ask B"]
    assert [m["content"] for m in third[1:]] == ["ask A", "Answer A.", "ask B", "Answer B.", "ask C"]
    assert [m["content"] for m in again[1:]] == ["ask A", "Answer A.", "ask B"]
    assert "ask C" not in str(again)


async def test_a_run_records_what_ran_settings_status_answer_and_trace(make_agent):
    agent = await make_agent(ScriptedLLM([TextChunk("Hello.")]))
    nb = agent.notebooks.create_notebook()
    cell = add_ai_cell(agent, nb, prompt="hi", model="no-model", effort=None, max_rows=77)
    events = await run(agent, nb, cell)

    assert any(isinstance(e, TextDelta) and e.text == "Hello." for e in events)
    assert any(isinstance(e, TraceReady) for e in events)
    r = run_of(agent, nb, 0)
    assert r["status"] == "ok" and r["prompt"] == "hi"
    assert r["settings"] == {"model": "no-model", "effort": None, "max_rows": 77}
    assert r["started_at"] <= r["finished_at"]
    assert [m["content"] for m in r["messages"]] == ["hi", "Hello."]
    assert r["trace"]["entries"][0]["kind"] == "llm_step"
    assert r["context_tokens"] == 100

    # editing the Draft does not touch the Run; a new Run replaces it, under a new id
    agent.notebooks.save_draft(nb, cell, replace(agent.notebooks.cell(nb, cell).draft, prompt="again"))
    assert run_of(agent, nb, 0)["prompt"] == "hi"
    agent.client_for = lambda model: ScriptedLLM([TextChunk("Second.")])
    await run(agent, nb, cell)
    r2 = run_of(agent, nb, 0)
    assert r2["id"] != r["id"] and [m["content"] for m in r2["messages"]] == ["again", "Second."]


async def test_the_system_prompt_lists_tables_from_cells_above_and_the_cells_own_earlier_tables(make_agent):
    llm = ScriptedLLM(
        [tool_call("run_query", sql=SQL, title="Ports")], [TextChunk("A done.")],       # cell A: two steps
        [tool_call("run_query", sql="SELECT sip FROM flows", title="Own1")],            # cell B, step 1
        [TextChunk("B done.")],                                                         # cell B, step 2
        [TextChunk("C done.")],                                                         # cell C
    )
    agent = await make_agent(llm)
    nb = agent.notebooks.create_notebook()
    a, b, c = (add_ai_cell(agent, nb, prompt=p) for p in "abc")
    for cell in (a, b, c):
        await run(agent, nb, cell)

    [a_table] = agent.tables.tables_of_run(a, run_of(agent, nb, 0)["id"])
    [b_table] = agent.tables.tables_of_run(b, run_of(agent, nb, 1)["id"])
    a_step1, a_step2, b_step1, b_step2, c_step = (call[0]["content"] for call in llm.calls)
    listed = lambda prompt: prompt.split("# Result Tables")[1]  # noqa: E731
    assert a_table.name not in a_step1
    assert a_table.name in listed(a_step2)                          # a cell's own table, later in its Run
    assert a_table.name in listed(b_step1) and b_table.name not in b_step1   # B sees A's table only
    assert listed(b_step2).index(a_table.name) < listed(b_step2).index(b_table.name)  # in creation order
    assert a_table.name in c_step and b_table.name in c_step
    assert a_table.owner == CellRun(a, run_of(agent, nb, 0)["id"])


async def test_stopping_a_cell_keeps_what_it_streamed_and_records_the_run_as_stopped(make_agent):
    agent = await make_agent(Slow())
    nb = agent.notebooks.create_notebook()
    a = add_ai_cell(agent, nb, prompt="a")
    async for ev in agent.notebooks.run_cell(nb, a):
        if isinstance(ev, TextDelta) and "Partial" in ev.text:
            assert agent.notebooks.stop_cell(nb, a)

    r = run_of(agent, nb, 0)
    assert r["status"] == "stopped"
    assert r["messages"][-1]["content"] == "Partial answer\n\n*Stopped by user.*"
    assert agent.notebooks.stop_cell(nb, a) is False

    scripted = ScriptedLLM([TextChunk("ok")])  # a cell below sees the stopped run
    agent.client_for = lambda model: scripted
    await run(agent, nb, add_ai_cell(agent, nb, prompt="b"))
    assert "Partial answer" in scripted.all_sent_text()


async def test_runs_are_numbered_in_the_order_they_start_within_a_notebook(make_agent):
    agent = await make_agent(ScriptedLLM([TextChunk("a")], [TextChunk("b")], [TextChunk("c")], [TextChunk("d")]))
    nb, other = agent.notebooks.create_notebook(), agent.notebooks.create_notebook()
    first, second = add_ai_cell(agent, nb, prompt="one"), add_ai_cell(agent, nb, prompt="two")
    elsewhere = add_ai_cell(agent, other, prompt="three")
    await run(agent, nb, first)
    await run(agent, other, elsewhere)
    await run(agent, nb, second)
    await run(agent, nb, first)  # a re-run takes the next number

    assert [run_of(agent, nb, i)["number"] for i in (0, 1)] == [3, 2]
    assert run_of(agent, other, 0)["number"] == 1


async def test_the_last_settings_run_are_those_of_the_most_recently_started_run(make_agent, notebook_store):
    agent = await make_agent(ScriptedLLM([TextChunk("a")], [TextChunk("b")]))
    assert notebook_store.last_settings() is None
    nb = agent.notebooks.create_notebook()
    one = add_ai_cell(agent, nb, prompt="one", effort="high", max_rows=50)
    two = add_ai_cell(agent, nb, prompt="two", effort="low", max_rows=7)
    await run(agent, nb, one)
    await run(agent, nb, two)
    assert notebook_store.last_settings() == run_of(agent, nb, 1)["settings"]
    assert notebook_store.last_settings()["max_rows"] == 7


def test_finishing_a_run_as_failed_needs_an_error_and_no_other_status_may_have_one(tmp_path):
    store = NotebookStore(tmp_path / "app.db")
    cell = store.add_ai_cell(store.create_notebook(), {"prompt": "q"})
    run_id = store.start_run(cell, "q", {}, "")
    with pytest.raises(ValueError):
        store.finish_run(run_id, "failed")
    for status in ("ok", "stopped"):
        with pytest.raises(ValueError):
            store.finish_run(run_id, status, "boom")
    with pytest.raises(ValueError):
        store.finish_run(run_id, "running")  # only starting a Run sets running
    assert store.cell(cell)["run"]["status"] == "running"

    store.finish_run(run_id, "failed", "The model gave no answer.")
    run = store.cell(cell)["run"]
    assert (run["status"], run["error"]) == ("failed", "The model gave no answer.")
