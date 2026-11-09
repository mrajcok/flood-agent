"""Refresh from a Conversation other than the creator's (ADR 0003, ADR 0006), at the HTTP seam: the one shared
table is refreshed for everyone, and ownership for lifecycle stays with the creator."""

import re

from conftest import DAY, Clock, ScriptedLLM, last_tool_result, tool_call
from test_conversations import ask
from test_refresh_table import HOUR, RECENT, refresh, rows
from test_show_other_conversations_tables import show_named_table

from flood_agent.llm import TextChunk


def two_conversations(build_agent, web_client, monkeypatch):
    """A creates a Result Table of recent flows and shows it; B shows A's table."""
    monkeypatch.setenv("FIXED_NOW_MS", "1700000003000")
    clock = Clock()
    llm = ScriptedLLM(
        [tool_call("run_query", sql=RECENT, title="t")],
        lambda msgs: [tool_call("show_table", result_table=last_tool_result(msgs)["result_table"])],
        [TextChunk("done")], show_named_table(), [TextChunk("there it is")])
    client = web_client(build_agent(llm, clock=clock, result_ttl_ms=DAY))
    a, _ = ask(client, "recent flows")
    table = re.search(r"/api/results/([a-z0-9_]+)/rows", client.get(f"/c/{a}").text).group(1)
    b, _ = ask(client, f"show {table}")
    return client, clock, a, b, table


def test_refreshing_a_table_shown_in_another_conversation_updates_the_shared_table(build_agent, web_client, monkeypatch):
    client, clock, a, b, table = two_conversations(build_agent, web_client, monkeypatch)
    assert rows(client, table)["total"] == 2
    assert client.get(f"/api/results/{table}/settings").json()["refreshable"] is True  # B's Display gets the control

    monkeypatch.setenv("FIXED_NOW_MS", "1700000003600")
    clock.ms += HOUR
    assert refresh(client, table).status_code == 200

    assert rows(client, table)["total"] == 1
    assert f"/api/results/{table}/rows" in client.get(f"/c/{a}").text  # the creator's Conversation still shows it
    assert f"/api/results/{table}/rows" in client.get(f"/c/{b}").text


def test_a_refresh_from_another_conversation_leaves_ownership_and_lifecycle_alone(build_agent, web_client, monkeypatch):
    client, clock, a, b, table = two_conversations(build_agent, web_client, monkeypatch)
    monkeypatch.setenv("FIXED_NOW_MS", "1700000003600")
    clock.ms += HOUR
    refresh(client, table)

    assert client.get(f"/c/{b}/delete-preview").json() == {"result": 0, "saved": 0}
    assert client.get(f"/c/{a}/delete-preview").json() == {"result": 1, "saved": 0}
    client.post(f"/c/{b}/delete", data={"tables": "delete_rt"})
    assert rows(client, table)["total"] == 1  # deleting B leaves it
    client.post(f"/c/{a}/delete", data={"tables": "delete_rt"})
    assert client.get(f"/api/results/{table}/rows").status_code == 410  # deleting the creator drops it as before
