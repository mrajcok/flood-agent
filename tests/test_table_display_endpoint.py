"""GET /api/tables/{name}/display: a Saved or Result Table as the chat's table Display, for the Tables
dialog's viewer (HTTP seam). Plain HTTP for the web page, never an MCP tool."""

from conftest import DAY, ScriptedLLM
from test_result_table_settings import shown_table
from test_show_tables import two_notebooks_with_tables


def test_a_saved_or_result_table_is_shown_as_the_chats_table_display(build_agent, web_client):
    client, _, _, a_table, b_table = two_notebooks_with_tables(build_agent, web_client)
    client.post(f"/api/results/{b_table}/settings", data={"name": "ddos_victims"})

    for name in (a_table, "ddos_victims"):
        r = client.get(f"/api/tables/{name}/display")
        assert r.status_code == 200 and r.headers["content-type"].startswith("text/html")
        assert f'data-rows-url="/api/results/{name}/rows"' in r.text
        assert f'data-psv-url="/api/results/{name}/psv"' in r.text
        assert f'data-settings-url="/api/results/{name}/settings"' in r.text
        assert client.get(f"/api/results/{name}/rows").status_code == 200  # the Display's rows load


def test_telemetry_lookup_and_unknown_tables_are_not_found(build_agent, web_client):
    client = web_client(build_agent(ScriptedLLM()))
    client.post("/api/lookup-tables", data={"name": "watch", "rows": "ip\n10.0.0.1"})

    for name in ("flows", "lookup_watch", "nope"):
        assert client.get(f"/api/tables/{name}/display").status_code == 404


def test_an_expired_table_is_gone(build_agent, web_client):
    client, clock, name = shown_table(build_agent, web_client)
    clock.ms += DAY + 1

    r = client.get(f"/api/tables/{name}/display")
    assert r.status_code == 410 and "expired" in r.json()["detail"].lower()
