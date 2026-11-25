"""An AI Cell Run starts from the AI Cell context above it, lists its Result Tables afresh before every LLM step,
and records them against its Cell and Run."""

from conftest import ScriptedLLM, add_ai_cell, add_markdown_cell, run_of, tool_call

from flood_agent.ai_cell_context import parse_system_prompt
from flood_agent.catalog import CellRun
from flood_agent.llm import TextChunk

SQL = "SELECT dport, SUM(bytes) AS total_bytes FROM flows GROUP BY dport ORDER BY total_bytes DESC"


async def test_an_ai_cell_run_starts_from_the_context_above_and_records_its_tables_against_its_cell_and_run(make_agent):
    llm = ScriptedLLM([TextChunk("Looking."), tool_call("run_query", sql=SQL, title="Ports")], [TextChunk("Done.")])
    agent = await make_agent(llm)
    nb = agent.notebooks.create_notebook()
    add_markdown_cell(agent, nb, "hi", send_to_ai=True)
    cell = add_ai_cell(agent, nb, prompt="ports please")

    [e async for e in agent.notebooks.run_cell(nb, cell)]

    first, second = llm.calls
    assert [(m["role"], m["content"]) for m in first[1:]] == [
        ("user", "[Markdown note] hi"), ("user", "ports please")]
    run = run_of(agent, nb)
    assert [m["role"] for m in run["messages"]] == ["user", "assistant", "tool", "assistant"]
    [table] = agent.tables.tables_of_run(cell, run["id"])
    assert table.owner == CellRun(cell, run["id"])
    # the Result Tables the system prompt lists are worked out again before every step
    assert table.name not in parse_system_prompt(first[0]["content"]).result_tables
    assert table.name in parse_system_prompt(second[0]["content"]).result_tables
