"""Restricted Columns through Cells: SQL Cell Runs, AI Cell tool calls and Refresh record them in the Table
Catalog, and the LLM is told which they are."""

import json
from pathlib import Path

import pytest
from conftest import ScriptedLLM, add_sql_cell, collect, last_tool_result, tool_call

from flood_agent.llm import TextChunk, ToolCallChunk

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
    assert table.restricted_columns == ("id",) and not table.lineage_unknown and table.restricted


async def test_a_sql_cell_run_records_unknown_lineage(agent):
    notebook = agent.notebooks.create_notebook()
    cell = await run_sql(agent, notebook, 'SELECT apn, "no such column" AS text FROM diameter')
    table = table_of(agent, notebook, cell)
    assert table.lineage_unknown and table.restricted and table.restricted_columns == ()


async def test_an_ai_cell_query_records_the_restricted_columns_without_telling_the_llm_in_query_metadata(agent, llm):
    llm.steps = [[tool_call("run_query", sql="SELECT msisdn AS phone, apn FROM diameter LIMIT 5", title="t")],
                 [TextChunk("ok")]]
    await collect(agent, "phones")
    metadata = last_tool_result(llm.calls[-1])
    assert agent.tables[metadata["result_table"]].restricted_columns == ("phone",)
    assert "restricted_columns" not in llm.all_sent_text()


async def test_refresh_and_a_sql_cell_re_run_record_the_lineage_again(agent):
    notebook = agent.notebooks.create_notebook()
    source_cell = await run_sql(agent, notebook, "SELECT apn AS v FROM diameter")
    source = table_of(agent, notebook, source_cell).name
    derived_cell = await run_sql(agent, notebook, f"SELECT v FROM {source}")
    derived = table_of(agent, notebook, derived_cell).name
    assert not agent.tables[derived].restricted

    await run_sql(agent, notebook, "SELECT imsi AS v FROM diameter", source_cell)
    assert agent.tables[source].restricted_columns == ("v",)
    assert not agent.tables[derived].restricted  # its rows are still the old ones

    assert agent.refresh_table(derived).restricted_columns == ("v",)


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
