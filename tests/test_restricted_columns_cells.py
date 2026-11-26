"""Restricted Columns through Cells: SQL Cell Runs, AI Cell tool calls and Refresh record them in the Table
Catalog, and the LLM is told which they are."""

import json
import threading
from pathlib import Path

import pytest
from conftest import ScriptedLLM, add_sql_cell, collect, last_tool_result, tool_call

from flood_agent.lineage import restriction
from flood_agent.llm import TextChunk, ToolCallChunk
from flood_agent.table_replacement import Rerun

pytestmark = pytest.mark.generated_data

REAL = Path(__file__).parent.parent / "semantic_layer.yaml"


@pytest.fixture
async def llm():
    return ScriptedLLM()


@pytest.fixture
def semantic_layer_path() -> Path:
    """The shipped Semantic Layer, which has the Restricted Columns, for this module's Agents."""
    return REAL


@pytest.fixture
async def agent(make_agent, llm):
    return await make_agent(llm)


async def run_sql(agent, notebook: str, sql: str, cell: str | None = None) -> str:
    """Run `sql` in a SQL Cell (a new one unless `cell` is given); returns the cell."""
    if cell is None:
        cell = add_sql_cell(agent, notebook, sql)
    else:
        draft = agent.notebooks.cell(notebook, cell).draft
        agent.notebooks.save_draft(notebook, cell, type(draft)(sql, draft.settings, draft.table))
    [event async for event in agent.notebooks.run_cell(notebook, cell)]
    return cell


def table_of(agent, notebook: str, cell: str):
    [table] = agent.tables.tables_of_cell(cell)
    return table


async def test_a_sql_cell_run_records_the_restricted_columns(agent):
    notebook = agent.notebooks.create_notebook()
    cell = await run_sql(agent, notebook, "SELECT imsi AS id, apn FROM diameter")
    table = table_of(agent, notebook, cell)
    assert table.restriction.columns == ("id",) and table.restricted


async def test_an_ai_cell_query_records_the_restricted_columns_without_telling_the_llm_in_query_metadata(agent, llm):
    llm.steps = [[tool_call("run_query", sql="SELECT msisdn AS phone, apn FROM diameter LIMIT 5", title="t")],
                 [TextChunk("ok")]]
    await collect(agent, "phones")
    metadata = last_tool_result(llm.calls[-1])
    assert agent.tables[metadata["result_table"]].restriction.columns == ("phone",)
    assert "restriction" not in llm.all_sent_text()


async def test_refresh_and_a_sql_cell_re_run_record_the_lineage_again(agent):
    notebook = agent.notebooks.create_notebook()
    source_cell = await run_sql(agent, notebook, "SELECT apn AS v FROM diameter")
    source = table_of(agent, notebook, source_cell).name
    derived_cell = await run_sql(agent, notebook, f"SELECT v FROM {source}")
    derived = table_of(agent, notebook, derived_cell).name
    assert not agent.tables[derived].restricted

    await run_sql(agent, notebook, "SELECT imsi AS v FROM diameter", source_cell)
    assert agent.tables[source].restriction.columns == ("v",)
    assert not agent.tables[derived].restricted  # its rows are still the old ones

    assert agent.refresh_table(derived).restriction.columns == ("v",)


async def test_the_llm_is_told_which_result_table_columns_are_restricted(agent, llm):
    notebook = agent.notebooks.create_notebook()
    llm.steps = [[tool_call("run_query", sql="SELECT imsi AS id, apn FROM diameter LIMIT 5", title="t")],
                 [TextChunk("ok")]]
    await collect(agent, "subscribers", notebook)
    name = last_tool_result(llm.calls[-1])["result_table"]
    assert f"- {name} (t): columns id TEXT (restricted), apn TEXT;" in llm.calls[-1][0]["content"]

    llm.steps = [[ToolCallChunk(id="c1", name="describe_table", arguments=json.dumps({"name": name}))], [TextChunk("ok")]]
    await collect(agent, "describe it")
    assert last_tool_result(llm.calls[-1])["columns"] == [
        {"name": "id", "type": "TEXT", "restricted": True}, {"name": "apn", "type": "TEXT"}]


async def test_the_llm_is_told_that_every_column_of_a_table_of_unknown_lineage_is_restricted(agent, llm):
    notebook = agent.notebooks.create_notebook()
    sql = """SELECT apn, "no such column" AS note FROM diameter LIMIT 5"""
    llm.steps = [[tool_call("run_query", sql=sql, title="t")], [TextChunk("ok")]]
    await collect(agent, "networks", notebook)
    name = last_tool_result(llm.calls[-1])["result_table"]
    assert f"- {name} (t): columns apn TEXT (restricted), note TEXT (restricted);" in llm.calls[-1][0]["content"]

    llm.steps = [[ToolCallChunk(id="c1", name="describe_table", arguments=json.dumps({"name": name}))], [TextChunk("ok")]]
    await collect(agent, "describe it")
    assert [c.get("restricted") for c in last_tool_result(llm.calls[-1])["columns"]] == [True, True]


async def restricted_source(agent) -> str:
    """A Result Table whose one column, `v`, is restricted."""
    notebook = agent.notebooks.create_notebook()
    return table_of(agent, notebook, await run_sql(agent, notebook, "SELECT imsi AS v FROM diameter")).name


def make_unrestricted(agent, table: str) -> None:
    """Replace `table`'s rows with ones holding nothing restricted, as its SQL Cell's re-run does."""
    agent.replacement.replace(table, "SELECT apn AS v FROM diameter", 50,
                              rerun=Rerun(agent.tables[table].owner, "T"))


async def test_a_query_reading_the_old_rows_during_a_replacement_does_not_take_the_new_restriction(
        agent, monkeypatch):
    source = await restricted_source(agent)
    swap, during = agent.queries.swap, []

    def query_then_swap(temp: str, name: str):  # the Catalog has been written; the old rows are still in place
        during.append(agent.queries.run(f"SELECT v FROM {source}", 50))
        swap(temp, name)

    monkeypatch.setattr(agent.queries, "swap", query_then_swap)
    make_unrestricted(agent, source)
    assert restriction(during[0]["lineage"]).restricted
    assert not agent.tables[source].restricted


async def test_a_source_cannot_be_replaced_between_a_query_reading_it_and_its_lineage(agent, query_runner):
    source = await restricted_source(agent)
    replacing = threading.Thread(target=make_unrestricted, args=(agent, source))

    def schema(name: str):  # lineage is being worked out: the rows have been read
        if replacing.ident is None:
            replacing.start()
            replacing.join(0.5)
        return agent.tables.schema(name)

    result = query_runner(agent.tables.readable, schema=schema).run(f"SELECT v FROM {source}", 50)
    replacing.join()
    assert restriction(result["lineage"]).restricted
    assert not agent.tables[source].restricted
