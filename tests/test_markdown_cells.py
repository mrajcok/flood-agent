"""Markdown Cells: storage, and what reaches the AI Cells below (only text whose Send to AI is ticked)."""

import re

from conftest import ScriptedLLM, add_ai_cell, add_markdown_cell, cell_ids, draft_of
from test_notebook_page import new_notebook

from flood_agent.events import FlagEvent
from flood_agent.llm import TextChunk
from flood_agent.semantic_layer import SYSTEM_INSTRUCTIONS


async def run(agent, nb, cell):
    return [ev async for ev in agent.notebooks.run_cell(nb, cell)]


async def test_a_value_only_in_an_unticked_note_is_flagged_but_a_ticked_one_is_seen(make_agent):
    llm = ScriptedLLM([TextChunk("Seen 203.0.113.9 and 198.51.100.7.")])
    agent = await make_agent(llm)
    nb = agent.notebooks.create_notebook()
    add_markdown_cell(agent, nb, "ticked: 203.0.113.9", send_to_ai=True)
    add_markdown_cell(agent, nb, "unticked: 198.51.100.7", send_to_ai=False)
    cell = add_ai_cell(agent, nb, prompt="ask")
    events = await run(agent, nb, cell)

    flagged = [t for e in events if isinstance(e, FlagEvent) for t in e.tokens]
    assert flagged == ["198.51.100.7"]


def test_the_instructions_explain_labelled_user_messages_and_never_vary():
    assert "[Markdown note]" in SYSTEM_INSTRUCTIONS
    assert "SHOW" in SYSTEM_INSTRUCTIONS and "DESCRIBE" in SYSTEM_INSTRUCTIONS


# --- the page's adapter ---------------------------------------------------------------------------------


def add_markdown(client, nb, after):
    r = client.post(f"/n/{nb}/cells", data={"kind": "markdown", "after": after})
    assert r.status_code == 200
    return r.text, re.search(r'data-cell-id="(\w+)" data-kind="markdown"', r.text).group(1)


def test_plus_markdown_inserts_an_unticked_empty_cell_after_the_given_one(build_agent, web_client):
    agent = build_agent(ScriptedLLM())
    client = web_client(agent)
    nb, ai = new_notebook(client)
    html, md = add_markdown(client, nb, ai)

    assert "Not sent to AI Cells below" in html
    assert 'data-insert="markdown"' in client.get(f"/n/{nb}").text  # the insert strip offers it
    assert cell_ids(agent, nb) == [ai, md]
    assert draft_of(agent, nb, md) == {"text": "", "send_to_ai": False, "displays": []}


def test_saving_a_markdown_cell_keeps_its_text_and_tick_and_shows_them_escaped_for_rendering(build_agent, web_client):
    agent = build_agent(ScriptedLLM())
    client = web_client(agent)
    nb, ai = new_notebook(client)
    _, md = add_markdown(client, nb, ai)

    r = client.post(f"/n/{nb}/cells/{md}/markdown", data={"text": "## Plan <b>x</b>", "send_to_ai": "1"})
    assert r.status_code == 204
    assert draft_of(agent, nb, md) == {"text": "## Plan <b>x</b>", "send_to_ai": True, "displays": []}
    shown = client.get(f"/n/{nb}/cells/{md}").text
    assert "## Plan &lt;b&gt;x&lt;/b&gt;" in shown and "<b>x</b>" not in shown

    client.post(f"/n/{nb}/cells/{md}/markdown", data={"text": "again"})
    assert draft_of(agent, nb, md) == {"text": "again", "send_to_ai": False, "displays": []}


def test_markdown_endpoint_rejects_an_ai_cell_and_unknown_kinds(build_agent, web_client):
    client = web_client(build_agent(ScriptedLLM()))
    nb, ai = new_notebook(client)
    assert client.post(f"/n/{nb}/cells/{ai}/markdown", data={"text": "x"}).status_code == 422
    assert client.post(f"/n/{nb}/cells", data={"kind": "nope"}).status_code == 422
