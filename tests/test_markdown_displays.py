"""Embedded Displays in Markdown Cells and Copy to Markdown Cell (#121): the copy survives a re-run, offers to save
`rt_` tables, follows renames, expires with its table, and sends the LLM nothing but the text and table names."""

import json
import re

import pytest
from conftest import ScriptedLLM, last_tool_result, tool_call
from test_notebook_page import new_notebook
from test_notebooks import run

from flood_agent.llm import TextChunk

SQL = "SELECT dport, bytes FROM flows WHERE bytes = 987654"
ENCODING = {"x": {"field": "dport", "type": "nominal"}, "y": {"field": "bytes", "type": "quantitative"}}


def answer_with_table_and_chart():
    """One Run: a query, then a table Display and a chart Display of it, then the answer."""
    return [
        [tool_call("run_query", sql=SQL, title="t")],
        lambda msgs: [tool_call("show_table", result_table=last_tool_result(msgs)["result_table"])],
        lambda msgs: [tool_call("show_chart", vega_lite_spec={
            "data": {"name": [json.loads(m["content"]) for m in msgs if m["role"] == "tool"][0]["result_table"]},
            "mark": "bar", "encoding": ENCODING})],
        [TextChunk("Port 8443 moved most.")],
    ]


async def ran_cell(make_agent, extra=()):
    llm = ScriptedLLM(*answer_with_table_and_chart(), *extra)
    agent = await make_agent(llm)
    nb = agent.create_notebook()
    cell = agent.add_ai_cell(nb, prompt="top ports")
    await run(agent, nb, cell)
    table = agent.notebooks.displays(agent.notebooks.cell(cell)["run"]["id"])[0]["result_table"]
    return agent, llm, nb, cell, table


async def test_copy_to_markdown_inserts_below_with_the_text_and_a_display_per_table_and_chart(make_agent):
    agent, _, nb, cell, table = await ran_cell(make_agent)
    new = agent.copy_to_markdown_cell(nb, cell)

    assert [c["id"] for c in agent.notebooks.cells(nb)] == [cell, new]
    draft = agent.notebooks.cell(new)["draft"]
    assert draft["text"] == "Port 8443 moved most."
    assert draft["send_to_ai"] is False
    assert [(d["kind"], d["table"]) for d in draft["displays"]] == [("table", table), ("chart", table)]
    assert draft["displays"][1]["spec"]["data"] == {"name": table}


async def test_the_copy_still_has_its_displays_after_the_ai_cell_is_re_run(make_agent):
    agent, _, nb, cell, table = await ran_cell(make_agent, extra=[[TextChunk("Different.")]])
    new = agent.copy_to_markdown_cell(nb, cell)
    await run(agent, nb, cell)  # the re-run replaces the Run (its Displays); the earlier table stays, for the copy
    draft = agent.notebooks.cell(new)["draft"]
    assert draft["text"] == "Port 8443 moved most." and len(draft["displays"]) == 2
    assert table in agent.tables


async def test_saving_renames_the_chosen_rt_tables_and_the_copy_uses_the_new_names(make_agent):
    agent, _, nb, cell, table = await ran_cell(make_agent)
    assert agent.markdown_copy_tables(nb, cell) == [table]
    new = agent.copy_to_markdown_cell(nb, cell, save={table: "top_ports"})

    assert agent.tables["top_ports"].kind == "saved" and table not in agent.tables
    assert {d["table"] for d in agent.notebooks.cell(new)["draft"]["displays"]} == {"top_ports"}
    # the AI Cell's own Displays followed the rename too
    run_displays = agent.notebooks.displays(agent.notebooks.cell(cell)["run"]["id"])
    assert {d["result_table"] for d in run_displays} == {"top_ports"}
    assert agent.markdown_copy_tables(nb, cell) == []


async def test_a_rename_elsewhere_updates_an_embedded_display_including_its_chart_spec(make_agent):
    agent, _, nb, cell, table = await ran_cell(make_agent)
    new = agent.copy_to_markdown_cell(nb, cell)
    agent.tables.update(table, new_name="renamed")
    displays = agent.notebooks.cell(new)["draft"]["displays"]
    assert [d["table"] for d in displays] == ["renamed", "renamed"]
    assert displays[1]["spec"]["data"] == {"name": "renamed"}


async def test_a_ticked_copy_sends_the_text_and_table_names_but_no_rows(make_agent):
    agent, llm, nb, cell, table = await ran_cell(make_agent, extra=[[TextChunk("ok")]])
    new = agent.copy_to_markdown_cell(nb, cell)
    agent.notebooks.edit_cell(new, send_to_ai=True)
    below = agent.add_ai_cell(nb, prompt="next")
    await run(agent, nb, below)
    note = [m["content"] for m in llm.calls[-1] if str(m.get("content")).startswith("[Markdown note]")]
    assert note == [f"[Markdown note] Port 8443 moved most.\n[Display: table {table}]\n[Display: chart {table}]"]
    assert "8443" not in note[0].replace("Port 8443 moved most.", "")


async def test_an_unticked_copy_sends_nothing(make_agent):
    agent, llm, nb, cell, _ = await ran_cell(make_agent, extra=[[TextChunk("ok")]])
    agent.copy_to_markdown_cell(nb, cell)
    below = agent.add_ai_cell(nb, prompt="next")
    await run(agent, nb, below)
    assert not any(str(m.get("content")).startswith("[Markdown note]") for m in llm.calls[-1])


async def test_a_bad_save_name_changes_nothing_and_adds_no_cell(make_agent):
    agent, _, nb, cell, table = await ran_cell(make_agent)
    with pytest.raises(ValueError):
        agent.copy_to_markdown_cell(nb, cell, save={table: "rt_nope"})
    assert len(agent.notebooks.cells(nb)) == 1 and table in agent.tables


def test_the_page_renders_embedded_displays_and_the_endpoints_copy(build_agent, web_client):
    llm = ScriptedLLM(*answer_with_table_and_chart())
    client = web_client(build_agent(llm))
    nb, cell = new_notebook(client)
    client.post(f"/n/{nb}/cells/{cell}/run", data={"prompt": "p", "model": "anthropic/claude-sonnet-5.5", "effort": "high", "max_rows": "1000"})
    client.get(f"/n/{nb}/cells/{cell}/stream")

    preview = client.get(f"/n/{nb}/cells/{cell}/copy-to-markdown").json()
    assert len(preview["tables"]) == 1 and preview["tables"][0].startswith("rt_")
    table = preview["tables"][0]
    bad = client.post(f"/n/{nb}/cells/{cell}/copy-to-markdown", data={"save": f"{table}:rt_x"})
    assert bad.status_code == 422
    r = client.post(f"/n/{nb}/cells/{cell}/copy-to-markdown", data={"save": f"{table}:keepers"})
    assert r.status_code == 200
    assert 'data-kind="markdown"' in r.text and "Port 8443 moved most." in r.text
    assert "/api/results/keepers/rows" in r.text and "/api/results/keepers/chart-rows" in r.text

    client.delete("/api/tables/keepers")
    page = client.get(f"/n/{nb}").text
    assert len(re.findall(r"<em>expired</em>", page)) >= 2
