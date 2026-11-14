"""Notebook management: rename, delete (keep or delete the `rt_` tables), search and ordering by activity."""

import asyncio

import pytest
from conftest import ScriptedLLM
from test_cell_operations import _cell_with_rt_and_saved
from test_notebook_page import new_notebook
from test_notebooks import Slow, run

from flood_agent.agent import NotebookBusy
from flood_agent.llm import TextChunk


async def test_rename_changes_the_title_but_not_the_activity_order(make_agent):
    agent = await make_agent(ScriptedLLM())
    first, second = agent.create_notebook(), agent.create_notebook()
    before = [n["id"] for n in agent.notebooks.list_notebooks()]
    agent.rename_notebook(first, "  Port scans  ")
    assert agent.notebooks.title_of(first) == "Port scans"
    assert [n["id"] for n in agent.notebooks.list_notebooks()] == before
    with pytest.raises(ValueError):
        agent.rename_notebook(first, "   ")
    with pytest.raises(KeyError):
        agent.rename_notebook("nope", "x")
    assert second in before


async def test_the_list_is_ordered_by_last_edit_or_run(make_agent):
    agent = await make_agent(ScriptedLLM([TextChunk("Hi.")]))
    old = agent.create_notebook()
    new = agent.create_notebook()
    a = agent.add_ai_cell(old, prompt="a")
    agent.notebooks._now = lambda: 10**13  # a later clock: the run touches `old`
    agent.notebooks.edit_cell(a, prompt="b")
    assert [n["id"] for n in agent.notebooks.list_notebooks()] == [old, new]
    agent.notebooks._now = lambda: 10**13 + 5
    agent.notebooks.edit_cell(agent.add_ai_cell(new, prompt="x"), prompt="y")
    assert [n["id"] for n in agent.notebooks.list_notebooks()] == [new, old]


async def test_search_matches_titles_and_cell_text_but_never_answers(make_agent):
    agent = await make_agent(ScriptedLLM([TextChunk("The zebra answer.")]))
    by_title = agent.create_notebook("Circuit A1B2 review")
    by_prompt, by_sql, by_note, by_answer = (agent.create_notebook() for _ in range(4))
    agent.add_ai_cell(by_prompt, prompt="why is circuit a1b2 slow?")
    agent.add_sql_cell(by_sql, sql="SELECT * FROM flows WHERE circuit = 'A1B2'")
    agent.add_markdown_cell(by_note, text="Notes on **a1b2**")
    cell = agent.add_ai_cell(by_answer, prompt="something else")
    await run(agent, by_answer, cell)
    found = {n["id"] for n in agent.notebooks.list_notebooks("A1b2")}
    assert found == {by_title, by_prompt, by_sql, by_note}
    assert agent.notebooks.list_notebooks("zebra") == []
    assert len(agent.notebooks.list_notebooks("  ")) == 5  # blank: everything


async def test_deleting_a_notebook_keeps_or_deletes_its_rt_tables_and_always_keeps_saved(make_agent):
    agent, nb, a, rt, current = await _cell_with_rt_and_saved(make_agent)
    assert agent.notebook_delete_preview(nb) == {"result": 2, "saved": 1}
    agent.delete_notebook(nb, "delete_rt")
    assert rt.name not in agent.tables and current.name not in agent.tables
    assert "kept_ports" in agent.tables
    assert not agent.notebooks.exists(nb)
    with pytest.raises(KeyError):
        agent.notebooks.cell(a)


async def test_deleting_with_the_keep_choice_leaves_the_tables_to_expire(make_agent):
    agent, nb, a, rt, current = await _cell_with_rt_and_saved(make_agent)
    agent.delete_notebook(nb, "keep")
    assert rt.name in agent.tables and current.name in agent.tables and "kept_ports" in agent.tables
    assert not agent.notebooks.exists(nb)


async def test_a_notebook_with_a_running_cell_cannot_be_deleted(make_agent):
    agent = await make_agent(Slow())
    nb = agent.create_notebook()
    a = agent.add_ai_cell(nb, prompt="a")
    task = asyncio.create_task(run(agent, nb, a))
    while agent.running_cell(nb) != a:
        await asyncio.sleep(0.01)
    with pytest.raises(NotebookBusy):
        agent.delete_notebook(nb)
    agent.stop_cell(a)
    await task
    agent.delete_notebook(nb)
    assert not agent.notebooks.exists(nb)


# --- through the page's routes -----------------------------------------------------------------------


def test_rename_route_retitles_and_rejects_a_blank_name(build_agent, web_client):
    agent = build_agent(ScriptedLLM())
    client = web_client(agent)
    nb, _ = new_notebook(client)
    r = client.post(f"/n/{nb}/rename", data={"title": "Port scans"}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == f"/n/{nb}"
    assert "Port scans" in client.get(f"/n/{nb}").text
    assert client.post(f"/n/{nb}/rename", data={"title": " "}).status_code == 422
    assert client.post("/n/nope/rename", data={"title": "x"}).status_code == 404


def test_delete_routes_preview_then_delete_and_go_home(build_agent, web_client):
    agent = build_agent(ScriptedLLM())
    client = web_client(agent)
    nb, _ = new_notebook(client)
    assert client.get(f"/n/{nb}/delete-preview").json() == {"result": 0, "saved": 0}
    r = client.post(f"/n/{nb}/delete", data={"tables": "keep"}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/"
    assert client.get(f"/n/{nb}").status_code == 404
    assert client.post(f"/n/{nb}/delete").status_code == 404
    assert client.get(f"/n/{nb}/delete-preview").status_code == 404


def test_search_route_returns_the_matching_notebooks(build_agent, web_client):
    agent = build_agent(ScriptedLLM())
    client = web_client(agent)
    nb, cell = new_notebook(client)
    other, _ = new_notebook(client)
    client.post(f"/n/{nb}/cells/{cell}/draft", data={"prompt": "circuit A1B2", "model": "", "effort": "", "max_rows": "10"})
    assert [n["id"] for n in client.get("/api/notebooks", params={"q": "a1b2"}).json()] == [nb]
    assert {n["id"] for n in client.get("/api/notebooks").json()} == {nb, other}


def test_the_sidebar_offers_search_rename_delete_and_hide(build_agent, web_client):
    agent = build_agent(ScriptedLLM())
    client = web_client(agent)
    nb, _ = new_notebook(client)
    html = client.get(f"/n/{nb}").text
    assert 'aria-label="Search notebooks"' in html
    assert f'action="/n/{nb}/rename"' in html
    assert f'data-preview-url="/n/{nb}/delete-preview"' in html
    assert 'aria-label="Hide sidebar"' in html and 'aria-label="Show sidebar"' in html
