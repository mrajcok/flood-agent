"""GET and DELETE /api/tables: the tables the user can query, for the Tables dialog (HTTP seam).

Plain HTTP for the web page, never an MCP tool, and names only (ADR 0001)."""

import re
import sqlite3

from conftest import DAY, Clock, ScriptedLLM
from test_conversations import ask, table_script


def table_of(client, cid):
    return re.search(r"/api/results/([a-z0-9_]+)/rows", client.get(f"/c/{cid}").text).group(1)


def two_conversations_with_tables(build_agent, web_client, **overrides):
    """Conversations A and B each create one Result Table. Returns (client, a, b, a_table, b_table)."""
    client = web_client(build_agent(ScriptedLLM(*table_script(), *table_script()), **overrides))
    a, _ = ask(client, "top ports")
    b, _ = ask(client, "top talkers")
    return client, a, b, table_of(client, a), table_of(client, b)


def test_telemetry_tables_are_those_described_in_the_semantic_layer(build_agent, web_client, semantic_layer_path):
    semantic_layer_path.write_text(semantic_layer_path.read_text() + "  dns:\n    description: DNS logs.\n")
    client = web_client(build_agent(ScriptedLLM()))
    r = client.get("/api/tables")
    assert r.status_code == 200
    assert r.json() == {"telemetry": ["dns", "flows"], "lookup": [], "saved": [], "result": [], "retention_days": {}}


def test_lookup_tables_are_listed_sorted(build_agent, web_client):
    client = web_client(build_agent(ScriptedLLM()))
    for name in ("watch", "bad_ips"):
        assert client.post("/api/lookup-tables", data={"name": name, "rows": "ip\n10.0.0.1"}).status_code == 200
    assert client.get("/api/tables").json()["lookup"] == ["lookup_bad_ips", "lookup_watch"]


def test_renamed_tables_are_listed_as_saved_tables_and_rt_ones_as_result_tables(build_agent, web_client):
    client, a, b, a_table, b_table = two_conversations_with_tables(build_agent, web_client)
    assert client.post(f"/api/results/{b_table}/settings", data={"name": "ddos_victims"}).status_code == 200
    assert client.post(f"/c/{a}/delete", data={"tables": "keep"}, follow_redirects=False).status_code == 303
    assert a_table.startswith("rt_")
    listed = client.get("/api/tables").json()
    assert listed["saved"] == ["ddos_victims"]
    assert listed["result"] == [a_table]


def test_saved_and_result_tables_come_with_their_retention_in_days(build_agent, web_client):
    client, _, _, a_table, b_table = two_conversations_with_tables(build_agent, web_client, result_ttl_ms=DAY)
    client.post("/api/lookup-tables", data={"name": "watch", "rows": "ip\n10.0.0.1"})
    client.post(f"/api/results/{b_table}/settings", data={"name": "ddos_victims", "retention_days": "30"})
    # Telemetry and Lookup Tables have no Retention; a Result Table not changed by the user has the default.
    assert client.get("/api/tables").json()["retention_days"] == {"ddos_victims": 30, a_table: 1}
    client.post(f"/api/results/{a_table}/settings", data={"retention_days": "0.5"})
    assert client.get("/api/tables").json()["retention_days"][a_table] == 0.5


def test_result_tables_past_their_retention_are_left_out(build_agent, web_client):
    clock = Clock()
    client = web_client(build_agent(ScriptedLLM(*table_script(), *table_script()), clock=clock, result_ttl_ms=DAY))
    a, _ = ask(client, "top ports")
    clock.ms += DAY // 2
    b, _ = ask(client, "top talkers")
    a_table, b_table = table_of(client, a), table_of(client, b)
    clock.ms += DAY // 2 + 1000  # A's table is past its Retention, not yet swept; B's is still live
    assert client.get("/api/tables").json()["result"] == [b_table]
    assert client.get(f"/api/results/{a_table}/rows").status_code == 410


def app_db_counts(path):
    """Conversations, turns, messages and saved Traces in the app database."""
    con = sqlite3.connect(path)
    try:
        return [con.execute(sql).fetchone()[0] for sql in (
            "SELECT COUNT(*) FROM conversations", "SELECT COUNT(*) FROM turns", "SELECT COUNT(*) FROM messages",
            "SELECT COUNT(*) FROM turns WHERE trace IS NOT NULL")]
    finally:
        con.close()


def test_listing_tables_creates_no_conversation_message_or_trace(build_agent, web_client, tmp_path):
    client, a, _, _, _ = two_conversations_with_tables(build_agent, web_client)
    counts = app_db_counts(tmp_path / "app.db")
    assert counts[0] == 2 and counts[3] == 2  # sanity: the setup did write Conversations and Traces
    home, conversation = client.get("/").text, client.get(f"/c/{a}").text
    assert client.get("/api/tables").status_code == 200
    assert app_db_counts(tmp_path / "app.db") == counts
    assert client.get("/").text == home
    assert client.get(f"/c/{a}").text == conversation


def test_deleting_a_lookup_table_drops_and_unregisters_it(build_agent, web_client):
    client = web_client(build_agent(ScriptedLLM()))
    client.post("/api/lookup-tables", data={"name": "watch", "rows": "ip\n10.0.0.1"})
    assert client.delete("/api/tables/lookup_watch").status_code == 204
    assert client.get("/api/tables").json()["lookup"] == []
    # Gone from the results database too: the name can be used again without Overwrite.
    assert client.post("/api/lookup-tables", data={"name": "watch", "rows": "ip\n10.0.0.2"}).status_code == 200


def test_deleting_saved_and_result_tables_leaves_their_displays_showing_expired(build_agent, web_client):
    client, a, b, a_table, b_table = two_conversations_with_tables(build_agent, web_client)
    client.post(f"/api/results/{b_table}/settings", data={"name": "ddos_victims"})
    assert client.delete(f"/api/tables/{a_table}").status_code == 204
    assert client.delete("/api/tables/ddos_victims").status_code == 204
    listed = client.get("/api/tables").json()
    assert listed["saved"] == [] and listed["result"] == []
    for name, cid in ((a_table, a), ("ddos_victims", b)):
        gone = client.get(f"/api/results/{name}/rows")
        assert gone.status_code == 410 and "expired" in gone.json()["detail"].lower()
        assert "data-rows-url" not in client.get(f"/c/{cid}").text


def test_telemetry_and_unknown_tables_cannot_be_deleted(build_agent, web_client):
    client, _, _, a_table, _ = two_conversations_with_tables(build_agent, web_client)
    assert client.delete("/api/tables/flows").status_code == 400
    assert client.delete("/api/tables/nope").status_code == 404
    assert client.delete(f"/api/tables/{a_table}").status_code == 204
    assert client.delete(f"/api/tables/{a_table}").status_code == 404  # already gone
    assert client.get("/api/tables").json()["telemetry"] == ["flows"]
