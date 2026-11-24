"""SQL Cells: run without the LLM, counts, re-run, Stop, errors, and what an AI Cell below is sent.

Driven through the SQL Cell Run module (`agent.sql_cells`) on an Agent that was never started: a SQL Cell needs no
MCP server. The tests of the Agent's own rules (one running Cell per Notebook, `stop_cell`, an AI Cell below) start
the Agent and go through `run_cell`."""

import asyncio
import contextlib
import logging
import sqlite3
import threading

import pytest
from conftest import DAY, Clock, ScriptedLLM

from flood_agent import queries
from flood_agent.catalog import CellRun
from flood_agent.events import CountEvent, DisplayEvent, TurnEnd
from flood_agent.llm import TextChunk
from flood_agent.notebooks import DEFAULT_MAX_ROWS

SELECT = "SELECT dport, SUM(bytes) AS total_bytes FROM flows GROUP BY dport ORDER BY total_bytes DESC"
SLOW = ("WITH RECURSIVE c(x) AS (SELECT 1 UNION ALL SELECT x + 1 FROM c WHERE x < 400000000) "
        "SELECT SUM(x) AS s FROM c")


async def run(agent, notebook_id, cell_id):
    """Run a Cell through the Agent (an AI Cell, or the Agent's rules for any Cell)."""
    return [ev async for ev in agent.run_cell(notebook_id, cell_id)]


async def run_sql(agent, notebook_id, cell_id):
    """Run a SQL Cell through the SQL Cell Run module."""
    return [ev async for ev in agent.sql_cells.run(agent.cell(cell_id))]


def run_of(agent, notebook_id, index):
    return agent.notebooks.cells(notebook_id)[index]["run"]


def table_of(agent, nb, index):
    cell = agent.notebooks.cells(nb)[index]
    [table] = agent.tables.tables_of_run(cell["id"], cell["run"]["id"])
    return table


def rows_of(agent, name):
    return agent.fetch_rows(name, page_size=1000)["rows"]


async def test_a_select_runs_without_the_llm_into_a_table_shown_as_a_display(build_agent):
    llm = ScriptedLLM()  # any LLM call would fail: it has no steps
    agent = build_agent(llm)  # never started: no MCP server
    nb = agent.create_notebook()
    cell = agent.add_sql_cell(nb, sql=SELECT, max_rows=2)
    events = await run_sql(agent, nb, cell)

    [display] = [e for e in events if isinstance(e, DisplayEvent)]
    assert isinstance(events[-1], TurnEnd) and llm.calls == []
    r = run_of(agent, nb, 0)
    table = table_of(agent, nb, 0)
    assert r["status"] == "ok" and r["number"] == 1
    assert display.result_table == table.name and table.name.startswith("rt_")
    assert table.owner == CellRun(cell, r["id"])
    assert table.title == "SELECT dport, SUM(bytes) AS total_bytes FROM flows GROUP BY…"  # the shortened SQL
    assert r["result"]["row_count"] == 2 and r["result"]["truncated_to"] == 2
    assert r["result"]["elapsed_s"] >= 0 and r["result"]["table"] == table.name
    assert len(rows_of(agent, table.name)) == 2
    assert [d["result_table"] for d in agent.notebooks.displays(r["id"])] == [table.name]


async def test_a_bare_count_gives_a_count_display_and_no_table(build_agent):
    agent = build_agent(ScriptedLLM())
    nb = agent.create_notebook()
    cell = agent.add_sql_cell(nb, sql="SELECT COUNT(*) FROM flows")
    events = await run_sql(agent, nb, cell)

    [count] = [e for e in events if isinstance(e, CountEvent)]
    r = run_of(agent, nb, 0)
    assert count.value > 0 and r["result"]["count"] == count.value
    assert agent.tables.tables_of_run(cell, r["id"]) == []
    [d] = agent.notebooks.displays(r["id"])
    assert d["kind"] == "count" and d["value"] == count.value


async def test_a_count_shaped_query_returning_many_rows_falls_back_to_a_table(build_agent):
    agent = build_agent(ScriptedLLM())
    nb = agent.create_notebook()
    cell = agent.add_sql_cell(nb, sql="SELECT COUNT(*) FROM flows GROUP BY dport")
    events = await run_sql(agent, nb, cell)

    assert [e for e in events if isinstance(e, DisplayEvent)] and not [e for e in events if isinstance(e, CountEvent)]
    assert run_of(agent, nb, 0)["status"] == "ok" and run_of(agent, nb, 0)["result"].get("count") is None
    assert len(rows_of(agent, table_of(agent, nb, 0).name)) > 1


async def test_statements_that_are_not_one_readable_select_are_refused(build_agent, tmp_path):
    agent = build_agent(ScriptedLLM())
    with sqlite3.connect(tmp_path / "results.db") as con:
        con.execute("CREATE TABLE secret(v)")
    nb = agent.create_notebook()
    for sql in ("DELETE FROM flows", "SELECT 1; SELECT 2", "SELECT * FROM results.secret", "SHOW TABLES"):
        cell = agent.add_sql_cell(nb, sql=sql)
        await run_sql(agent, nb, cell)
        r = agent.notebooks.cell(cell)["run"]
        assert r["status"] == "failed", sql
        assert agent.tables.tables_of_run(cell, r["id"]) == []
        assert r["error"]


def test_a_query_ending_in_limit_is_recognised_but_a_limit_in_a_subquery_is_not():
    for sql in ("SELECT dport FROM flows LIMIT 5", "select dport from flows limit 5;", "SELECT 1 FROM flows LIMIT 5 OFFSET 10\n",
                "SELECT 1 FROM flows LIMIT 10, 5"):
        assert queries.ends_with_limit(sql), sql
    for sql in ("SELECT dport FROM flows", "SELECT dport FROM flows WHERE dport IN (SELECT dport FROM flows LIMIT 5)",
                "SELECT 'limit 5' AS note FROM flows"):
        assert not queries.ends_with_limit(sql), sql


async def test_a_query_ending_in_limit_is_refused_with_a_pointer_to_the_row_cap(build_agent):
    agent = build_agent(ScriptedLLM())
    nb = agent.create_notebook()
    cell = agent.add_sql_cell(nb, sql="SELECT dport FROM flows LIMIT 5")
    await run_sql(agent, nb, cell)
    r = agent.notebooks.cell(cell)["run"]
    assert r["status"] == "failed" and "row cap" in r["error"]
    assert agent.tables.tables_of_run(cell, r["id"]) == []


async def test_an_old_sql_cell_saved_without_a_row_cap_runs_with_the_default_row_cap(build_agent):
    agent = build_agent(ScriptedLLM())
    nb = agent.create_notebook()
    cell = agent.notebooks.add_cell(nb, "sql", {"sql": "SELECT dport FROM flows"})
    await run(agent, nb, cell)

    r = run_of(agent, nb, 0)
    assert r["status"] == "ok" and r["settings"] == {"max_rows": DEFAULT_MAX_ROWS, "command": ""}


async def test_re_running_replaces_the_table_under_the_same_name(build_agent):
    agent = build_agent(ScriptedLLM())
    nb = agent.create_notebook()
    cell = agent.add_sql_cell(nb, sql="SELECT dport FROM flows", max_rows=2)
    await run_sql(agent, nb, cell)
    first = table_of(agent, nb, 0)
    agent.notebooks.edit_cell(cell, sql="SELECT sip, dport FROM flows", max_rows=3)
    await run_sql(agent, nb, cell)

    second = table_of(agent, nb, 0)
    assert second.name == first.name and second.owner == CellRun(cell, run_of(agent, nb, 0)["id"])
    assert [c["name"] for c in second.columns] == ["sip", "dport"] and len(rows_of(agent, second.name)) == 3
    assert second.sql == "SELECT sip, dport FROM flows" and second.max_rows == 3
    assert run_of(agent, nb, 0)["number"] == 2
    assert len(agent.tables.names()) == 1


async def test_a_failing_re_run_leaves_the_old_table_and_rows_untouched(build_agent):
    agent = build_agent(ScriptedLLM())
    nb = agent.create_notebook()
    cell = agent.add_sql_cell(nb, sql="SELECT dport FROM flows", max_rows=2)
    await run_sql(agent, nb, cell)
    before_run = run_of(agent, nb, 0)["id"]
    old = agent.tables[table_of(agent, nb, 0).name]
    old_rows = rows_of(agent, old.name)
    agent.notebooks.edit_cell(cell, sql="SELECT nosuchcolumn FROM flows")
    await run_sql(agent, nb, cell)

    assert run_of(agent, nb, 0)["status"] == "failed"
    now = agent.tables[old.name]
    assert now.columns == old.columns and now.sql == old.sql and now.owner == CellRun(cell, before_run)
    assert rows_of(agent, old.name) == old_rows and agent.tables.names() == [old.name]


async def until_running(agent, nb, index):
    for _ in range(100):
        await asyncio.sleep(0.05)
        if (run_of(agent, nb, index) or {}).get("status") == "running":
            break
    await asyncio.sleep(0.2)  # the query is under way


async def test_stop_ends_a_running_sql_cell_and_leaves_the_old_table_untouched(build_agent):
    agent = build_agent(ScriptedLLM(), query_timeout_s=60)
    nb = agent.create_notebook()
    cell = agent.add_sql_cell(nb, sql="SELECT dport FROM flows", max_rows=2)
    await run_sql(agent, nb, cell)
    old = table_of(agent, nb, 0)
    old_rows = rows_of(agent, old.name)
    agent.notebooks.edit_cell(cell, sql=SLOW)

    task = asyncio.create_task(run_sql(agent, nb, cell))
    await until_running(agent, nb, 0)
    assert agent.sql_cells.stop(cell)
    await asyncio.wait_for(task, 10)

    assert run_of(agent, nb, 0)["status"] == "stopped"
    assert agent.tables[old.name].sql == "SELECT dport FROM flows" and rows_of(agent, old.name) == old_rows
    assert agent.sql_cells.stop(cell) is False  # no longer running


async def test_a_client_that_leaves_mid_query_abandons_it_and_the_run_is_stopped(build_agent):
    agent = build_agent(ScriptedLLM(), query_timeout_s=60)
    nb = agent.create_notebook()
    cell = agent.add_sql_cell(nb, sql=SLOW)
    task = asyncio.create_task(run(agent, nb, cell))
    await until_running(agent, nb, 0)

    task.cancel()  # the page was closed: the stream is abandoned
    with contextlib.suppress(asyncio.CancelledError):
        await asyncio.wait_for(task, 10)

    assert run_of(agent, nb, 0)["status"] == "stopped" and agent.sql_cells.stop(cell) is False


async def test_what_is_saved_with_the_run_and_sent_below_keeps_its_exact_shape(build_agent):
    """These are stored formats: the page and saved Notebooks read the result, and AI Cells below were sent the
    message. The expected values were taken from the code before the SQL Cell Run module existed."""
    agent = build_agent(ScriptedLLM())
    nb = agent.create_notebook()
    table_cell = agent.add_sql_cell(nb, sql="SELECT dport FROM flows ORDER BY dport", max_rows=2)
    show_cell = agent.add_sql_cell(nb, command="DESCRIBE TABLE", table="flows")
    await run_sql(agent, nb, table_cell)
    await run_sql(agent, nb, show_cell)

    def saved(index):
        run_ = run_of(agent, nb, index)
        result = dict(run_["result"])
        assert result.pop("elapsed_s") >= 0
        return list(result.items()), run_["messages"][-1]["content"]

    name = table_of(agent, nb, 0).name
    assert saved(0) == (
        [("table", name), ("row_count", 2), ("truncated_to", 2)],
        "[SQL Cell, run by the user] SELECT dport FROM flows ORDER BY dport\nQuery Metadata: "
        f'{{"result_table": "{name}", "columns": [{{"name": "dport", "type": "INT"}}], "row_count": 2, "truncated_to": 2}}',
    )
    columns = [("sip", "TEXT"), ("dip", "TEXT"), ("sport", "INTEGER"), ("dport", "INTEGER"), ("proto", "INTEGER"),
               ("tcp_flags", "INTEGER"), ("circuit", "TEXT"), ("stime", "INTEGER"), ("etime", "INTEGER"),
               ("bytes", "INTEGER"), ("pkts", "INTEGER")]
    rows = [[n, t, i] for i, (n, t) in enumerate(columns, 1)]
    assert saved(1) == (
        [("columns", ["column_name", "data_type", "ordinal_position"]), ("rows", rows),
         ("row_count", 11), ("command", "DESCRIBE TABLE")],
        "[SHOW Cell, run by the user] DESCRIBE TABLE flows\ncolumn_name | data_type | ordinal_position\n"
        + "\n".join(f"{n} | {t} | {i}" for n, t, i in rows),
    )


async def test_an_ai_cell_below_sees_the_labelled_query_with_metadata_frozen_at_the_run(make_agent):
    llm = ScriptedLLM([TextChunk("Seen.")])
    agent = await make_agent(llm)
    nb = agent.create_notebook()
    sql_cell = agent.add_sql_cell(nb, sql=SELECT, max_rows=500)
    ai = agent.add_ai_cell(nb, prompt="what next?")
    await run(agent, nb, sql_cell)
    table = table_of(agent, nb, 0)
    agent.tables.update(table.name, new_name="my_ports")  # a later rename: the cell below still sees the name as it was
    await run(agent, nb, ai)

    system, user, question = llm.calls[0][0]["content"], llm.calls[0][1]["content"], llm.calls[0][2]["content"]
    assert user.startswith(f"[SQL Cell, run by the user] {SELECT}")
    assert '"dport"' in user and '"total_bytes"' in user and '"row_count"' in user and table.name in user
    assert question == "what next?"
    assert "my_ports (SQL Cell) " in system and "SUM(bytes)" in system
    assert "never" in system.lower() and "labelled" in system  # the labelled-messages paragraph


async def test_only_a_count_that_passed_the_count_form_check_reaches_the_ai_cell(make_agent):
    llm = ScriptedLLM([TextChunk("ok")])
    agent = await make_agent(llm)
    nb = agent.create_notebook()
    counted = agent.add_sql_cell(nb, sql="SELECT COUNT(*) FROM flows")
    grouped = agent.add_sql_cell(nb, sql="SELECT COUNT(*) AS n FROM flows GROUP BY dport")
    ai = agent.add_ai_cell(nb, prompt="q")
    for c in (counted, grouped):
        await run(agent, nb, c)
    value = run_of(agent, nb, 0)["result"]["count"]
    await run(agent, nb, ai)

    first, second = llm.calls[0][1]["content"], llm.calls[0][2]["content"]
    assert f"Count: {value}" in first and "Count:" not in second and "row_count" in second


async def test_the_user_sees_the_raw_error_the_ai_cell_below_the_value_free_one_and_a_warning_is_logged(make_agent, caplog, tmp_path):
    llm = ScriptedLLM([TextChunk("ok")])
    agent = await make_agent(llm)
    with sqlite3.connect(tmp_path / "results.db") as con:
        con.execute("CREATE TABLE nosuchcolumn_7(v)")  # unregistered, so unreadable: the raw denial names it
    nb = agent.create_notebook()
    bad = agent.add_sql_cell(nb, sql="SELECT * FROM results.nosuchcolumn_7")
    ai = agent.add_ai_cell(nb, prompt="q")
    with caplog.at_level(logging.WARNING, logger="uvicorn.error"):
        await run(agent, nb, bad)
    await run(agent, nb, ai)

    r = run_of(agent, nb, 0)
    assert r["status"] == "failed" and "nosuchcolumn_7" in r["result"]["raw_error"]
    assert r["error"] and "nosuchcolumn_7" not in r["error"]  # the Run keeps the value-free one
    assert "status" not in r["result"] and "error" not in r["result"]  # each said once, on the Run
    seen = llm.calls[0][1]["content"]
    assert seen.startswith("[SQL Cell, run by the user] SELECT * FROM results.nosuchcolumn_7")
    assert f"Failed: {r['error']}" in seen
    assert "nosuchcolumn_7" not in seen.split("\n", 1)[1]  # past the SQL itself, the error carries no names
    assert any("nosuchcolumn_7" in rec.getMessage() and rec.levelno == logging.WARNING for rec in caplog.records)


async def test_a_second_cell_cannot_run_while_a_sql_cell_runs_and_the_agents_stop_reaches_it(make_agent):
    import pytest

    from flood_agent.agent import NotebookBusy
    agent = await make_agent(ScriptedLLM(), query_timeout_s=60)
    nb = agent.create_notebook()
    slow = agent.add_sql_cell(nb, sql=SLOW)
    other = agent.add_sql_cell(nb, sql="SELECT 1 AS one")
    task = asyncio.create_task(run(agent, nb, slow))
    await asyncio.sleep(0.3)
    with pytest.raises(NotebookBusy):
        await run(agent, nb, other)
    assert agent.running_cell(nb) == slow
    assert agent.stop_cell(slow)  # the Agent's Stop reaches a running SQL Cell
    await asyncio.wait_for(task, 10)
    assert run_of(agent, nb, 0)["status"] == "stopped" and agent.running_cell(nb) is None


async def test_a_re_run_never_overwrites_a_saved_table_it_makes_a_new_one(build_agent):
    agent = build_agent(ScriptedLLM())
    nb = agent.create_notebook()
    cell = agent.add_sql_cell(nb, sql="SELECT dport FROM flows", max_rows=2)
    await run_sql(agent, nb, cell)
    first = table_of(agent, nb, 0)
    agent.tables.update(first.name, new_name="my_ports")  # saved: no longer a Result Table
    saved_rows = rows_of(agent, "my_ports")
    agent.notebooks.edit_cell(cell, sql="SELECT sip FROM flows", max_rows=3)
    await run_sql(agent, nb, cell)

    saved = agent.tables["my_ports"]
    assert saved.kind == "saved" and saved.sql == "SELECT dport FROM flows" and rows_of(agent, "my_ports") == saved_rows
    new = table_of(agent, nb, 0)
    assert new.name != "my_ports" and new.kind == "result" and new.sql == "SELECT sip FROM flows"


async def test_a_re_run_after_a_run_that_made_no_table_creates_a_new_table(build_agent):
    agent = build_agent(ScriptedLLM())
    nb = agent.create_notebook()
    cell = agent.add_sql_cell(nb, sql="SELECT dport FROM flows", max_rows=2)
    await run_sql(agent, nb, cell)
    old = table_of(agent, nb, 0)
    agent.notebooks.edit_cell(cell, sql="SELECT nosuchcolumn FROM flows")
    await run_sql(agent, nb, cell)  # failed: the latest Run made no table
    agent.notebooks.edit_cell(cell, sql="SELECT sip FROM flows")
    await run_sql(agent, nb, cell)

    new = table_of(agent, nb, 0)
    assert new.name != old.name and agent.tables[old.name].sql == "SELECT dport FROM flows"


async def test_a_stopped_sql_cell_reaches_the_ai_cell_below_as_stopped_by_the_user(make_agent):
    llm = ScriptedLLM([TextChunk("ok")])
    agent = await make_agent(llm, query_timeout_s=60)
    nb = agent.create_notebook()
    cell = agent.add_sql_cell(nb, sql=SLOW)
    ai = agent.add_ai_cell(nb, prompt="q")
    task = asyncio.create_task(run(agent, nb, cell))
    for _ in range(100):
        await asyncio.sleep(0.05)
        if agent.running_cell(nb):
            break
    await asyncio.sleep(0.2)
    agent.stop_cell(cell)
    await asyncio.wait_for(task, 10)
    await run(agent, nb, ai)

    assert llm.calls[0][1]["content"] == f"[SQL Cell, run by the user] {SLOW}\n(stopped by the user)"


async def test_the_context_roles_when_notes_and_sql_and_show_cells_precede_an_ai_prompt(make_agent):
    llm = ScriptedLLM([TextChunk("ok")])
    agent = await make_agent(llm)
    nb = agent.create_notebook()
    agent.add_markdown_cell(nb, text="a note", send_to_ai=True)
    sql = agent.add_sql_cell(nb, sql="SELECT dport FROM flows", max_rows=2)
    show = agent.add_sql_cell(nb, command="SHOW TABLES")
    ai = agent.add_ai_cell(nb, prompt="q")
    await run(agent, nb, sql)
    await run(agent, nb, show)
    await run(agent, nb, ai)

    # Consecutive user messages are intentional (and valid for OpenRouter): each cell is its own message.
    assert [m["role"] for m in llm.calls[0]] == ["system", "user", "user", "user", "user"]
    contents = [m["content"] for m in llm.calls[0][1:]]
    assert "a note" in contents[0]
    assert contents[1].startswith("[SQL Cell, run by the user]") and contents[2].startswith("[SHOW Cell, run by the user]")
    assert contents[3] == "q"


async def test_a_table_swept_during_a_re_run_fails_the_cell_telling_the_user_to_run_it_again(build_agent, monkeypatch):
    clock = Clock()
    agent = build_agent(ScriptedLLM(), clock=clock, result_ttl_ms=DAY)
    nb = agent.create_notebook()
    cell = agent.add_sql_cell(nb, sql="SELECT dport FROM flows", max_rows=2)
    await run_sql(agent, nb, cell)
    real = queries.run_query

    def sweep_during(*args, **kwargs):
        result = real(*args, **kwargs)
        clock.ms += 2 * DAY
        agent.tables.sweep()  # the table's Retention is over while the query runs
        return result

    monkeypatch.setattr(queries, "run_query", sweep_during)
    await run_sql(agent, nb, cell)

    run_ = run_of(agent, nb, 0)
    assert run_["status"] == "failed"
    assert "expired during the run" in run_["result"]["raw_error"] and "run the cell again" in run_["result"]["raw_error"].lower()
    assert agent.tables.names() == []  # no new table was made in its place


async def test_the_agent_runs_a_sql_cell_without_being_started_but_not_an_ai_cell(build_agent):
    import pytest

    agent = build_agent(ScriptedLLM())  # never started: no MCP server
    nb = agent.create_notebook()
    sql = agent.add_sql_cell(nb, sql="SELECT 1 AS one")
    ai = agent.add_ai_cell(nb, prompt="q")
    events = await run(agent, nb, sql)
    assert isinstance(events[-1], TurnEnd) and run_of(agent, nb, 0)["status"] == "ok"
    assert agent.running_cell(nb) is None
    with pytest.raises(RuntimeError, match="not started"):
        await run(agent, nb, ai)
    assert agent.running_cell(nb) is None  # the refused AI Cell did not leave the Notebook busy


async def test_a_re_run_after_the_table_expired_creates_a_fresh_table(build_agent):
    clock = Clock()
    agent = build_agent(ScriptedLLM(), clock=clock, result_ttl_ms=DAY)
    nb = agent.create_notebook()
    cell = agent.add_sql_cell(nb, sql="SELECT dport FROM flows", max_rows=2)
    await run_sql(agent, nb, cell)
    old = table_of(agent, nb, 0).name
    clock.ms += 2 * DAY
    agent.tables.sweep()  # its Retention is over before the re-run
    await asyncio.sleep(1.1)  # table names come from the wall-clock second: the fake clock moved, the real one did not

    await run_sql(agent, nb, cell)

    assert run_of(agent, nb, 0)["status"] == "ok"
    new = table_of(agent, nb, 0)
    assert new.name != old and agent.tables.names() == [new.name]


async def test_a_re_run_while_its_table_is_being_refreshed_fails_saying_so(build_agent, monkeypatch):
    agent = build_agent(ScriptedLLM())
    nb = agent.create_notebook()
    cell = agent.add_sql_cell(nb, sql="SELECT dport FROM flows", max_rows=2)
    await run_sql(agent, nb, cell)
    name = table_of(agent, nb, 0).name
    real, entered, release = queries.run_query, threading.Event(), threading.Event()

    def held(*args, **kwargs):  # the Refresh's query, held open until the re-run has tried
        entered.set()
        release.wait(5)
        return real(*args, **kwargs)

    monkeypatch.setattr(queries, "run_query", held)
    refresh = threading.Thread(target=agent.refresh_table, args=(name,))
    refresh.start()
    assert entered.wait(5)
    try:
        await run_sql(agent, nb, cell)
    finally:
        release.set()
        refresh.join(5)

    run_ = run_of(agent, nb, 0)
    assert run_["status"] == "failed" and "already being replaced" in run_["result"]["raw_error"]
    assert agent.tables.names() == [name]


async def test_the_frozen_message_for_a_count_a_failure_and_a_stop(build_agent):
    agent = build_agent(ScriptedLLM(), query_timeout_s=60)
    nb = agent.create_notebook()
    count = agent.add_sql_cell(nb, sql="SELECT COUNT(*) FROM flows")
    limited = agent.add_sql_cell(nb, sql="SELECT dport FROM flows LIMIT 5")
    slow = agent.add_sql_cell(nb, sql=SLOW)
    await run_sql(agent, nb, count)
    await run_sql(agent, nb, limited)
    task = asyncio.create_task(run_sql(agent, nb, slow))
    await until_running(agent, nb, 2)
    agent.sql_cells.stop(slow)
    await asyncio.wait_for(task, 10)

    def message(index):
        return run_of(agent, nb, index)["messages"][-1]["content"]

    value = run_of(agent, nb, 0)["result"]["count"]
    assert message(0) == f"[SQL Cell, run by the user] SELECT COUNT(*) FROM flows\nCount: {value}"
    assert message(1) == f"[SQL Cell, run by the user] SELECT dport FROM flows LIMIT 5\nFailed: {queries.LIMIT_IN_SQL_CELL}"
    assert message(2) == f"[SQL Cell, run by the user] {SLOW}\n(stopped by the user)"


async def test_closing_the_agent_stops_a_running_sql_cell_and_waits_for_its_query(build_agent):
    agent = build_agent(ScriptedLLM(), query_timeout_s=60)
    nb = agent.create_notebook()
    cell = agent.add_sql_cell(nb, sql=SLOW)
    task = asyncio.create_task(run_sql(agent, nb, cell))
    await until_running(agent, nb, 0)

    await asyncio.wait_for(agent.aclose(), 10)  # the query is aborted, not left running

    await asyncio.wait_for(task, 10)
    assert run_of(agent, nb, 0)["status"] == "stopped"


async def test_a_sql_cell_cannot_run_once_the_agent_is_closed(build_agent):
    agent = build_agent(ScriptedLLM())
    nb = agent.create_notebook()
    cell = agent.add_sql_cell(nb, sql=SELECT)
    await agent.aclose()

    with pytest.raises(RuntimeError, match="closed"):
        await run(agent, nb, cell)
    assert agent.notebooks.cell(cell)["run"] is None  # no Run was started
