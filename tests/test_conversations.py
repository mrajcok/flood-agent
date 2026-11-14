"""Conversation-only UI: each turn collapsing under its prompt, and its x with a confirmation. Both go with
Conversations (#125); everything else that was tested here is covered by the notebook tests
(test_notebook_page.py, test_notebook_management.py, test_cell_operations*.py)."""

import re

from conftest import ScriptedLLM, main_pane

from flood_agent.llm import TextChunk


def ask(client, message, conversation_id=""):
    posted = client.post("/chat", data={"message": message, "conversation_id": conversation_id, "max_rows": "1000"})
    assert posted.status_code == 200
    cid = re.search(r'name="conversation_id" value="([0-9a-f]+)"', posted.text).group(1)
    stream_url = posted.text.split('sse-connect="')[1].split('"')[0]
    return cid, client.get(stream_url).text


def test_each_prompt_and_its_response_collapse_under_the_prompt(build_agent, web_client):
    client = web_client(build_agent(ScriptedLLM([TextChunk("reply-alpha")], [TextChunk("reply-beta")])))
    cid, _ = ask(client, "first question")
    live = client.post("/chat", data={"message": "second question", "conversation_id": cid, "max_rows": "1000"}).text
    ask_stream = live.split('sse-connect="')[1].split('"')[0]
    client.get(ask_stream)
    assert 'class="turn" open' in live and '<summary class="user"><span>second question</span>' in live

    page = main_pane(client.get(f"/c/{cid}").text)
    turns = page.split('<details class="turn" open>')[1:]
    assert len(turns) == 2
    assert turns[0].startswith('<summary class="user"><span>first question</span>') and "reply-alpha" in turns[0]
    assert "reply-beta" not in turns[0] and "second question" not in turns[0]
    assert turns[1].startswith('<summary class="user"><span>second question</span>') and "reply-beta" in turns[1]


def test_each_prompt_has_an_x_with_a_confirmation_both_live_and_on_reopen(build_agent, web_client):
    client = web_client(build_agent(ScriptedLLM([TextChunk("hi")])))
    cid, body = ask(client, "hello")
    assert "event: turn" in body and "/turns/" in body  # the live turn gets its x once its id is known

    page = main_pane(client.get(f"/c/{cid}").text)
    summary = page.split("<summary")[1].split("</summary>")[0]
    assert "hello" in summary and 'action="/c/' in summary and "&times;" in summary
    assert "data-preview-url" in summary and "data-title" in summary
