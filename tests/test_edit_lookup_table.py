"""Editing a Lookup Table from the Tables dialog (plain HTTP, never MCP): fetch its rows as text in a
chosen separator, re-format edited text, and save the edits over the table."""

from conftest import ScriptedLLM
from test_lookup_tables import add, stored


def rows_of(client, name, sep):
    return client.get(f"/api/lookup-tables/{name}", params={"sep": sep})


def test_rows_come_back_in_the_chosen_separator_with_quoting_and_nulls_as_empty(build_agent, web_client):
    client = web_client(build_agent(ScriptedLLM()))
    add(client, "hosts", 'ip,desc\n10.0.0.1,"edge, east"\n10.0.0.2,\n10.0.0.3,a|b')
    assert rows_of(client, "lookup_hosts", "comma").json() == {
        "name": "lookup_hosts", "text": 'ip,desc\n10.0.0.1,"edge, east"\n10.0.0.2,\n10.0.0.3,a|b\n'}
    assert rows_of(client, "lookup_hosts", "pipe").json()["text"] == (
        'ip|desc\n10.0.0.1|edge, east\n10.0.0.2|\n10.0.0.3|"a|b"\n')


def test_only_lookup_tables_and_known_separators_are_served(build_agent, web_client):
    client = web_client(build_agent(ScriptedLLM()))
    add(client, "hosts", "ip\n10.0.0.1")
    assert rows_of(client, "flows", "comma").status_code == 404  # a Telemetry table
    assert rows_of(client, "lookup_nope", "comma").status_code == 404
    assert rows_of(client, "LOOKUP_HOSTS", "comma").json()["name"] == "lookup_hosts"
    for sep in ("tab", "semicolon"):  # a tab cannot be typed in the edit dialog's textarea
        r = rows_of(client, "lookup_hosts", sep)
        assert r.status_code == 422 and "comma, pipe" in r.json()["detail"]


def reformat(client, rows, sep):
    return client.post("/api/lookup-tables/reformat", data={"rows": rows, "sep": sep})


def test_edited_text_even_pasted_tab_separated_is_reformatted_in_another_separator(build_agent, web_client):
    client = web_client(build_agent(ScriptedLLM()))
    r = reformat(client, "ip\tdesc\n10.0.0.9\tedge, west\n\n10.0.0.8\t", "comma")
    assert r.json() == {"text": 'ip,desc\n10.0.0.9,"edge, west"\n10.0.0.8,\n'}


def test_text_that_does_not_parse_is_not_reformatted(build_agent, web_client):
    client = web_client(build_agent(ScriptedLLM()))
    r = reformat(client, "ip,desc\n10.0.0.9", "pipe")
    assert r.status_code == 422 and "Check line 2" in r.json()["detail"]


def save(client, name, rows):
    return client.put(f"/api/lookup-tables/{name}", data={"rows": rows})


def test_saving_replaces_the_rows_and_columns(build_agent, web_client):
    agent = build_agent(ScriptedLLM())
    client = web_client(agent)
    add(client, "hosts", "ip\n10.0.0.1")
    r = save(client, "lookup_hosts", "ip|owner\n10.0.0.5|ops\n10.0.0.6|")
    assert r.json() == {"name": "lookup_hosts", "row_count": 2, "columns": ["ip", "owner"]}
    assert stored(agent, "lookup_hosts") == [["10.0.0.5", "ops"], ["10.0.0.6", None]]
    assert agent.list_tables()["lookup"] == ["lookup_hosts"]
    assert rows_of(client, "lookup_hosts", "comma").json()["text"] == "ip,owner\n10.0.0.5,ops\n10.0.0.6,\n"


def test_saving_rows_that_do_not_parse_keeps_the_old_rows(build_agent, web_client):
    agent = build_agent(ScriptedLLM())
    client = web_client(agent)
    add(client, "hosts", "ip\n10.0.0.1")
    r = save(client, "lookup_hosts", "ip,ip\n1,2")
    assert r.status_code == 422 and "different" in r.json()["detail"]
    assert stored(agent, "lookup_hosts") == [["10.0.0.1"]]


def test_only_an_existing_lookup_table_can_be_saved_over(build_agent, web_client):
    agent = build_agent(ScriptedLLM())
    client = web_client(agent)
    assert save(client, "lookup_gone", "ip\n10.0.0.1").status_code == 404
    assert save(client, "flows", "ip\n10.0.0.1").status_code == 404
    assert agent.list_tables()["lookup"] == []
