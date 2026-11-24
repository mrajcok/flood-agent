"""Drafts, AI Cell re-runs, Runs that did not finish as cells below see them, and tables from earlier Runs."""


from conftest import ScriptedLLM, tool_call
from test_notebooks import SQL, Slow

from flood_agent.agent import TextDelta, TurnError
from flood_agent.llm import LLMError, TextChunk


async def run(agent, nb, cell):
    return [ev async for ev in agent.run_cell(nb, cell)]


async def test_a_cell_below_is_sent_the_prompt_that_ran_not_the_edited_draft(make_agent):
    llm = ScriptedLLM([TextChunk("Answer A.")], [TextChunk("Answer B.")])
    agent = await make_agent(llm)
    nb = agent.create_notebook()
    a, b = agent.add_ai_cell(nb, prompt="ask A"), agent.add_ai_cell(nb, prompt="ask B")
    await run(agent, nb, a)
    agent.notebooks.edit_ai_cell(a, prompt="something else entirely")
    await run(agent, nb, b)

    assert [m["content"] for m in llm.calls[1][1:]] == ["ask A", "Answer A.", "ask B"]


async def test_a_stopped_run_reaches_the_cell_below_as_what_streamed_plus_stopped_by_the_user(make_agent):
    agent = await make_agent(Slow())
    nb = agent.create_notebook()
    a, b = agent.add_ai_cell(nb, prompt="ask A"), agent.add_ai_cell(nb, prompt="ask B")
    async for ev in agent.run_cell(nb, a):
        if isinstance(ev, TextDelta) and "Partial" in ev.text:
            agent.stop_cell(a)
    scripted = ScriptedLLM([TextChunk("ok")])
    agent.client_for = lambda model: scripted
    await run(agent, nb, b)

    assert [m["content"] for m in scripted.calls[0][1:]] == [
        "ask A", "Partial answer\n\n(stopped by the user)", "ask B"]


async def test_a_failed_run_reaches_the_cell_below_as_failed_with_a_value_free_error(make_agent):
    def boom(messages):
        raise LLMError("The model provider reported an error: upstream exploded")

    agent = await make_agent(ScriptedLLM(boom))
    nb = agent.create_notebook()
    a, b = agent.add_ai_cell(nb, prompt="ask A"), agent.add_ai_cell(nb, prompt="ask B")
    (shown,) = [e.message for e in await run(agent, nb, a) if isinstance(e, TurnError)]
    failed = agent.notebooks.cell(a)["run"]
    assert (failed["status"], failed["error"]) == ("failed", shown)
    assert "upstream exploded" in shown
    scripted = ScriptedLLM([TextChunk("ok")])
    agent.client_for = lambda model: scripted
    await run(agent, nb, b)

    assert [m["content"] for m in scripted.calls[0][1:]] == ["ask A", f"failed: {shown}", "ask B"]


async def test_a_failure_that_is_not_the_models_is_stored_as_the_message_the_analyst_was_shown(make_agent):
    def boom(messages):
        raise RuntimeError("secret value 10.1.2.3")

    agent = await make_agent(ScriptedLLM(boom))
    nb = agent.create_notebook()
    a = agent.add_ai_cell(nb, prompt="ask A")
    (shown,) = [e.message for e in await run(agent, nb, a) if isinstance(e, TurnError)]
    failed = agent.notebooks.cell(a)["run"]
    assert (failed["status"], failed["error"]) == ("failed", shown)
    assert "10.1.2.3" not in failed["error"]  # value-free


async def test_a_rerun_leaves_earlier_tables_out_of_every_system_prompt_but_keeps_them_owned(make_agent):
    llm = ScriptedLLM(
        [tool_call("run_query", sql=SQL, title="First")], [TextChunk("one")],
        [tool_call("run_query", sql="SELECT sip FROM flows", title="Second")], [TextChunk("two")],
        [TextChunk("below")])
    agent = await make_agent(llm)
    nb = agent.create_notebook()
    a, b = agent.add_ai_cell(nb, prompt="a"), agent.add_ai_cell(nb, prompt="b")
    await run(agent, nb, a)
    [old] = agent.tables.tables_of_run(a, agent.notebooks.cell(a)["run"]["id"])
    await run(agent, nb, a)
    await run(agent, nb, b)

    listed = f"- {old.name} ("
    assert listed not in llm.calls[3][0]["content"] and listed not in llm.calls[4][0]["content"]
    assert [t.name for t in agent.tables.earlier_tables_of_cell(a, agent.notebooks.cell(a)["run"]["id"])] == [old.name]
    assert old.name in agent.tables  # still live until its Retention ends


async def test_deleting_earlier_rt_tables_spares_saved_tables_and_the_current_run(make_agent):
    llm = ScriptedLLM(
        [tool_call("run_query", sql=SQL, title="First", call_id="c1"),
         tool_call("run_query", sql="SELECT sip FROM flows", title="Kept", call_id="c2")], [TextChunk("one")],
        [tool_call("run_query", sql=SQL, title="Now")], [TextChunk("two")])
    agent = await make_agent(llm)
    nb = agent.create_notebook()
    a = agent.add_ai_cell(nb, prompt="a")
    await run(agent, nb, a)
    first_run = agent.notebooks.cell(a)["run"]["id"]
    rt, keep = agent.tables.tables_of_run(a, first_run)
    agent.tables.update(keep.name, new_name="kept_ports")
    await run(agent, nb, a)
    [current] = agent.tables.tables_of_run(a, agent.notebooks.cell(a)["run"]["id"])

    dropped = agent.tables.drop_earlier_of_cell(a, agent.notebooks.cell(a)["run"]["id"])

    assert dropped == [rt.name]
    assert rt.name not in agent.tables and "kept_ports" in agent.tables and current.name in agent.tables
    assert [t.name for t in agent.tables.earlier_tables_of_cell(a, agent.notebooks.cell(a)["run"]["id"])] == ["kept_ports"]
