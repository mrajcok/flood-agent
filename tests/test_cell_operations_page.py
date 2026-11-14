"""Cell operations through the page's routes."""

import re

from conftest import ScriptedLLM
from test_markdown_cells import add_markdown
from test_notebook_page import new_notebook


def order(agent, nb):
    return [c["id"] for c in agent.notebooks.cells(nb)]


def test_at_start_inserts_above_the_first_cell(build_agent, web_client):
    agent = build_agent(ScriptedLLM())
    client = web_client(agent)
    nb, ai = new_notebook(client)
    r = client.post(f"/n/{nb}/cells", data={"kind": "markdown", "at": "start"})
    top = re.search(r'data-cell-id="(\w+)" data-kind="markdown"', r.text).group(1)
    assert order(agent, nb) == [top, ai]
    assert 'data-add="sql"' in client.get(f"/n/{nb}").text


def test_move_copy_and_delete_persist(build_agent, web_client):
    agent = build_agent(ScriptedLLM())
    client = web_client(agent)
    nb, ai = new_notebook(client)
    _, md = add_markdown(client, nb, ai)
    client.post(f"/n/{nb}/cells/{md}/markdown", data={"text": "note", "send_to_ai": "1"})

    assert client.post(f"/n/{nb}/cells/{md}/move", data={"direction": "up"}).status_code == 204
    assert order(agent, nb) == [md, ai]

    r = client.post(f"/n/{nb}/cells/{md}/copy")
    copy = re.search(r'data-cell-id="(\w+)" data-kind="markdown"', r.text).group(1)
    assert order(agent, nb) == [md, copy, ai]
    assert agent.notebooks.cell(copy)["draft"] == {"text": "note", "send_to_ai": True}

    assert client.get(f"/n/{nb}/cells/{copy}/delete-preview").json() == {"result": 0, "saved": 0}
    assert client.post(f"/n/{nb}/cells/{copy}/delete", data={"tables": "keep"}).status_code == 204
    assert order(agent, nb) == [md, ai]
    assert client.get(f"/n/{nb}/cells/{copy}").status_code == 404
    assert client.post(f"/n/{nb}/cells/{copy}/delete").status_code == 404
