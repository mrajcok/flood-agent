"""SHOW / DESCRIBE SQL Cells (the Command drop-down) through the Agent's Notebook API."""

from conftest import ScriptedLLM
from test_sql_cells import SELECT, run, run_of

from flood_agent import agent as agent_module
from flood_agent.agent import TurnEnd, TurnError
from flood_agent.llm import TextChunk
from flood_agent.show_commands import SHOW_COMMANDS

NOT_AVAILABLE = "not available on the development database"


async def ran_show(make_agent, command, table=""):
    agent = await make_agent(ScriptedLLM())
    nb = agent.create_notebook()
    cell = agent.add_sql_cell(nb, command=command, table=table)
    events = await run(agent, nb, cell)
    return agent, nb, cell, events


def column(result, name):
    return [row[result["columns"].index(name)] for row in result["rows"]]


async def test_show_tables_lists_the_readable_tables_with_no_table_made(make_agent):
    agent = await make_agent(ScriptedLLM())
    nb = agent.create_notebook()
    await run(agent, nb, agent.add_sql_cell(nb, sql=SELECT))
    made = agent.tables.names()
    show = agent.add_sql_cell(nb, command="SHOW TABLES")
    events = await run(agent, nb, show)

    r = run_of(agent, nb, 1)
    assert r["status"] == "ok" and isinstance(events[-1], TurnEnd)
    names = column(r["result"], "table_name")
    assert "flows" in names and made[0] in names
    assert r["result"]["row_count"] == len(names)
    assert agent.tables.names() == made
    assert agent.tables.tables_of_run(show, r["id"]) == []


async def test_show_columns_and_describe_table(make_agent):
    agent, nb, cell, _ = await ran_show(make_agent, "SHOW COLUMNS")
    res = run_of(agent, nb, 0)["result"]
    flows = [c for t, c in zip(column(res, "table_name"), column(res, "column_name")) if t == "flows"]
    assert "dport" in flows

    agent, nb, cell, _ = await ran_show(make_agent, "DESCRIBE TABLE", "flows")
    res = run_of(agent, nb, 0)["result"]
    assert res["status"] == "ok" and {"dport", "bytes"} <= set(column(res, "column_name"))


async def test_describe_of_an_unreadable_table_fails(make_agent):
    agent, nb, cell, events = await ran_show(make_agent, "DESCRIBE TABLE", "sqlite_master")
    r = run_of(agent, nb, 0)
    assert r["status"] == "failed" and any(isinstance(e, TurnError) for e in events)
    assert "sqlite_master" in r["result"]["raw_error"]


async def test_the_other_commands_are_not_available_on_the_development_database(make_agent):
    assert len(SHOW_COMMANDS) == 17
    agent, nb, cell, _ = await ran_show(make_agent, "SHOW USERS")
    r = run_of(agent, nb, 0)
    assert r["status"] == "failed" and NOT_AVAILABLE in r["result"]["raw_error"]


async def test_there_is_no_row_cap_and_rows_are_kept_with_the_run(make_agent):
    agent = await make_agent(ScriptedLLM())
    nb = agent.create_notebook()
    await run(agent, nb, agent.add_sql_cell(nb, command="SHOW COLUMNS", max_rows=1))
    res = run_of(agent, nb, 0)["result"]
    assert res["row_count"] > 1 and len(res["rows"]) == res["row_count"] and not res.get("truncated_to")


async def test_show_or_describe_typed_into_the_textbox_is_refused_with_a_pointer(make_agent):
    for sql in ("SHOW TABLES", "  describe table flows", "show\ncolumns"):
        agent = await make_agent(ScriptedLLM())
        nb = agent.create_notebook()
        await run(agent, nb, agent.add_sql_cell(nb, sql=sql))
        r = run_of(agent, nb, 0)
        assert r["status"] == "failed" and "Command drop-down" in r["result"]["raw_error"]


async def test_an_ai_cell_below_sees_the_statement_and_only_some_rows_with_a_note(make_agent, monkeypatch):
    monkeypatch.setattr(agent_module, "SHOW_ROWS_TO_AI", 3)
    llm = ScriptedLLM([TextChunk("ok")])
    agent = await make_agent(llm)
    nb = agent.create_notebook()
    show = agent.add_sql_cell(nb, command="SHOW COLUMNS")
    ai = agent.add_ai_cell(nb, prompt="q")
    await run(agent, nb, show)
    total = run_of(agent, nb, 0)["result"]["row_count"]
    await run(agent, nb, ai)
    user = llm.calls[0][1]["content"]
    assert total > 3
    assert user.startswith("[SHOW Cell, run by the user] SHOW COLUMNS")
    assert f"3 of {total} rows" in user and user.count("\n") <= 6


async def test_a_small_result_is_sent_whole_with_no_note(make_agent):
    llm = ScriptedLLM([TextChunk("ok")])
    agent = await make_agent(llm)
    nb = agent.create_notebook()
    show = agent.add_sql_cell(nb, command="DESCRIBE TABLE", table="flows")
    ai = agent.add_ai_cell(nb, prompt="q")
    await run(agent, nb, show)
    await run(agent, nb, ai)
    user = llm.calls[0][1]["content"]
    assert user.startswith("[SHOW Cell, run by the user] DESCRIBE TABLE flows")
    assert "dport" in user and "left out" not in user
