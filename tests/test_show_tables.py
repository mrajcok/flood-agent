"""GET /api/tables: the names of every table the user can query, for the Show tables dialog (HTTP seam).

Plain HTTP for the web page, never an MCP tool, and names only (ADR 0001)."""

import re

from conftest import ScriptedLLM
from test_conversations import ask, table_script

DAY = 24 * 3_600_000


class Clock:
    def __init__(self, ms=1_000_000):
        self.ms = ms

    def __call__(self):
        return self.ms


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
    assert r.json() == {"telemetry": ["dns", "flows"], "lookup": [], "result": []}


def test_lookup_tables_are_listed_sorted(build_agent, web_client):
    client = web_client(build_agent(ScriptedLLM()))
    for name in ("watch", "bad_ips"):
        assert client.post("/api/lookup-tables", data={"name": name, "rows": "ip\n10.0.0.1"}).status_code == 200
    assert client.get("/api/tables").json()["lookup"] == ["lookup_bad_ips", "lookup_watch"]


def test_live_result_tables_from_every_conversation_are_listed_by_current_name(build_agent, web_client):
    client, a, b, a_table, b_table = two_conversations_with_tables(build_agent, web_client)
    assert client.post(f"/api/results/{b_table}/settings", data={"name": "ddos_victims"}).status_code == 200
    assert client.post(f"/c/{a}/delete", data={"tables": "keep"}, follow_redirects=False).status_code == 303
    assert a_table.startswith("rt_")
    assert client.get("/api/tables").json()["result"] == ["ddos_victims", a_table]


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


def test_listing_tables_creates_no_conversation_message_or_trace(build_agent, web_client):
    client, a, _, _, _ = two_conversations_with_tables(build_agent, web_client)
    home, conversation = client.get("/").text, client.get(f"/c/{a}").text
    assert client.get("/api/tables").status_code == 200
    assert client.get("/").text == home
    assert client.get(f"/c/{a}").text == conversation
