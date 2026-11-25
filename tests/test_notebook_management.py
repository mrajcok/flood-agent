"""Notebook management: ordering by activity, search, and the rename, delete and search routes. Trimming, blank
titles and deleting with its tables are tested in test_notebook.py."""

from dataclasses import replace

from conftest import Clock, ScriptedLLM, add_ai_cell, add_markdown_cell, add_sql_cell
from test_notebook_page import new_notebook
from test_notebooks import run

from flood_agent.llm import TextChunk


async def test_rename_changes_the_title_but_not_the_activity_order(make_agent):
    agent = await make_agent(ScriptedLLM())
    first, second = agent.notebooks.create_notebook(), agent.notebooks.create_notebook()
    before = [n["id"] for n in agent.notebooks.list_notebooks()]
    agent.notebooks.rename_notebook(first, "  Port scans  ")
    assert agent.notebooks.notebook_page(first).title == "Port scans"
    assert [n["id"] for n in agent.notebooks.list_notebooks()] == before
    assert second in before


async def test_the_list_is_ordered_by_last_edit_or_run(make_agent):
    clock = Clock()
    agent = await make_agent(ScriptedLLM([TextChunk("Hi.")]), clock=clock)
    old = agent.notebooks.create_notebook()
    clock.ms += 1
    new = agent.notebooks.create_notebook()
    a = add_ai_cell(agent, old, prompt="a")
    clock.ms += 1  # a later clock: the edit touches `old`
    agent.notebooks.save_draft(old, a, replace(agent.notebooks.cell(old, a).draft, prompt="b"))
    assert [n["id"] for n in agent.notebooks.list_notebooks()] == [old, new]
    clock.ms += 1
    x = add_ai_cell(agent, new, prompt="x")
    agent.notebooks.save_draft(new, x, replace(agent.notebooks.cell(new, x).draft, prompt="y"))
    assert [n["id"] for n in agent.notebooks.list_notebooks()] == [new, old]


async def test_search_matches_titles_and_cell_text_but_never_answers(make_agent):
    agent = await make_agent(ScriptedLLM([TextChunk("The zebra answer.")]))
    by_title = agent.notebooks.create_notebook("Circuit A1B2 review")
    by_prompt, by_sql, by_note, by_answer = (agent.notebooks.create_notebook() for _ in range(4))
    add_ai_cell(agent, by_prompt, prompt="why is circuit a1b2 slow?")
    add_sql_cell(agent, by_sql, sql="SELECT * FROM flows WHERE circuit = 'A1B2'")
    add_markdown_cell(agent, by_note, text="Notes on **a1b2**")
    cell = add_ai_cell(agent, by_answer, prompt="something else")
    await run(agent, by_answer, cell)
    found = {n["id"] for n in agent.notebooks.list_notebooks("A1b2")}
    assert found == {by_title, by_prompt, by_sql, by_note}
    assert agent.notebooks.list_notebooks("zebra") == []
    assert len(agent.notebooks.list_notebooks("  ")) == 5  # blank: everything


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
