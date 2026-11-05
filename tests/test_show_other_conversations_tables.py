"""Showing another Conversation's live Result Table (ADR 0003), at the HTTP seam.

The Display is saved with the Conversation that shows it; the table stays owned by the
Conversation that created it."""

import re

from conftest import ScriptedLLM, last_tool_result, tool_call
from test_conversations import ask, main_pane, table_script, turn_ids

from flood_agent.llm import TextChunk


def show_named_table():
    """A scripted step that shows the table named by the last word of the user's message."""
    return lambda msgs: [tool_call("show_table", result_table=msgs[-1]["content"].split()[-1])]


def two_conversations(build_agent, web_client):
    """A creates a Result Table and shows it; B shows A's table. Returns (client, a, b, table)."""
    client = web_client(build_agent(ScriptedLLM(*table_script(), show_named_table(), [TextChunk("there it is")])))
    a, _ = ask(client, "top ports")
    table = re.search(r"/api/results/([a-z0-9_]+)/rows", client.get(f"/c/{a}").text).group(1)
    b, body = ask(client, f"show {table}")
    assert f"/api/results/{table}/rows" in body
    return client, a, b, table


def test_b_saves_the_display_but_deleting_b_never_drops_as_table(build_agent, web_client):
    client, a, b, table = two_conversations(build_agent, web_client)
    assert f"/api/results/{table}/rows" in client.get(f"/c/{b}").text

    assert client.get(f"/c/{b}/delete-preview").json() == {"result": 0, "saved": 0}
    client.post(f"/c/{b}/delete", data={"tables": "delete_rt"})
    assert client.get(f"/api/results/{table}/rows").status_code == 200
    assert f"/api/results/{table}/rows" in client.get(f"/c/{a}").text


def test_deleting_bs_turn_that_showed_as_table_leaves_the_table(build_agent, web_client):
    client, a, b, table = two_conversations(build_agent, web_client)
    (turn,) = turn_ids(client.get(f"/c/{b}").text)
    assert client.get(f"/c/{b}/delete-preview?turn_id={turn}").json() == {"result": 0, "saved": 0}
    client.post(f"/c/{b}/turns/{turn}/delete", data={"tables": "delete_rt"})
    assert client.get(f"/api/results/{table}/rows").status_code == 200


def test_when_a_is_deleted_with_its_tables_bs_display_shows_expired(build_agent, web_client):
    client, a, b, table = two_conversations(build_agent, web_client)
    client.post(f"/c/{a}/delete", data={"tables": "delete_rt"})

    gone = client.get(f"/api/results/{table}/rows")
    assert gone.status_code == 410 and "expired" in gone.json()["detail"].lower()
    page = main_pane(client.get(f"/c/{b}").text)
    assert "expired" in page.lower() and "data-rows-url" not in page


def test_renaming_as_table_is_followed_by_bs_display(build_agent, web_client):
    client, a, b, table = two_conversations(build_agent, web_client)
    renamed = client.post(f"/api/results/{table}/settings", data={"name": "port_totals", "retention_days": "1"})
    assert renamed.status_code == 200
    page = client.get(f"/c/{b}").text
    assert "/api/results/port_totals/rows" in page and f"/api/results/{table}/rows" not in page
    assert client.get("/api/results/port_totals/rows").status_code == 200


def test_showing_a_lookup_table_points_the_llm_at_querying_it(build_agent, web_client):
    llm = ScriptedLLM([tool_call("show_table", result_table="lookup_watch")], [TextChunk("ok")])
    client = web_client(build_agent(llm))
    assert client.post("/api/lookup-tables", data={"name": "watch", "rows": "ip\n10.0.0.1"}).status_code == 200
    ask(client, "show lookup_watch")
    error = last_tool_result(llm.calls[-1])["error"]
    assert "Lookup Table" in error and "run_query" in error
