"""Choosing what happens to Result Tables when deleting a notebook or one cell (HTTP seam)."""

import re

from conftest import DAY, Clock, ScriptedLLM, ask, last_tool_result, table_script, tool_call

from flood_agent.catalog import CellRun
from flood_agent.llm import TextChunk


def tables_in(client, nb):
    return re.findall(r"/api/results/([a-z0-9_]+)/rows", client.get(f"/n/{nb}").text)


def two_tables(build_agent, web_client, *extra):
    """One notebook, two AI Cells, one table each; the second table is renamed.
    Returns (client, clock, notebook, first cell, second cell, first table, second table)."""
    clock = Clock()
    agent = build_agent(ScriptedLLM(*table_script("a"), *table_script("b"), *extra), clock=clock, result_ttl_ms=DAY)
    client = web_client(agent)
    nb, c1, _ = ask(client, "first")
    clock.ms += 2000  # distinct creation times
    _, c2, _ = ask(client, "second", nb)
    first, second = tables_in(client, nb)[:2]
    client.post(f"/api/results/{second}/settings", data={"name": "keeper"})
    return client, clock, nb, c1, c2, first, "keeper"


def readable(client, name):
    return client.get(f"/api/results/{name}/rows").status_code == 200


def test_preview_counts_live_result_tables_and_saved_tables_separately(build_agent, web_client):
    client, _, nb, c1, c2, _, _ = two_tables(build_agent, web_client)
    assert client.get(f"/n/{nb}/delete-preview").json() == {"result": 1, "saved": 1}
    assert client.get(f"/n/{nb}/cells/{c1}/delete-preview").json() == {"result": 1, "saved": 0}
    assert client.get(f"/n/{nb}/cells/{c2}/delete-preview").json() == {"result": 0, "saved": 1}
    assert client.get("/n/nope/delete-preview").status_code == 404
    assert client.get(f"/n/{nb}/cells/nope/delete-preview").status_code == 404


def test_deleting_a_notebook_keeps_its_tables_by_default_until_they_expire(build_agent, web_client):
    client, clock, nb, _, _, first, second = two_tables(build_agent, web_client)
    assert client.post(f"/n/{nb}/delete", follow_redirects=False).status_code == 303
    assert readable(client, first) and readable(client, second)
    clock.ms += DAY + 10_000
    assert client.get(f"/api/results/{first}/rows").status_code == 410


def test_a_kept_table_is_queryable_from_another_notebook(build_agent, web_client):
    seen = {}

    def query_kept(msgs):
        return [tool_call("run_query", sql=f"SELECT * FROM {seen['name']}", title="again")]

    client, _, nb, _, _, first, _ = two_tables(build_agent, web_client, query_kept, lambda msgs: [
        TextChunk("ok" if last_tool_result(msgs).get("success") else "failed")])
    seen["name"] = first
    client.post(f"/n/{nb}/delete", data={"tables": "keep"})
    _, _, stream = ask(client, "from elsewhere")
    assert "ok" in stream and "failed" not in stream


def test_deleting_result_tables_drops_only_rt_tables_and_keeps_saved_tables(build_agent, web_client):
    client, _, nb, _, _, first, second = two_tables(build_agent, web_client)
    client.post(f"/n/{nb}/delete", data={"tables": "delete_rt"})
    assert not readable(client, first) and readable(client, second)


def test_delete_all_is_not_a_choice(build_agent, web_client):
    client, _, nb, c1, _, first, second = two_tables(build_agent, web_client)
    assert client.post(f"/n/{nb}/delete", data={"tables": "delete_all"}).status_code == 422
    assert client.post(f"/n/{nb}/cells/{c1}/delete", data={"tables": "delete_all"}).status_code == 422
    assert readable(client, first) and readable(client, second)


def test_unknown_choice_is_rejected_and_deletes_nothing(build_agent, web_client):
    client, _, nb, _, _, first, _ = two_tables(build_agent, web_client)
    assert client.post(f"/n/{nb}/delete", data={"tables": "bogus"}).status_code == 422
    assert readable(client, first) and f'href="/n/{nb}"' in client.get("/").text


def test_cell_deletion_keeps_by_default_and_honours_the_choices(build_agent, web_client):
    client, _, nb, c1, c2, first, second = two_tables(build_agent, web_client)
    client.post(f"/n/{nb}/cells/{c1}/delete")
    assert readable(client, first)
    client.post(f"/n/{nb}/cells/{c2}/delete", data={"tables": "delete_rt"})
    assert readable(client, second)  # renamed, so delete_rt keeps it


def test_cell_delete_rt_drops_that_cells_result_tables_only(build_agent, web_client):
    client, _, nb, c1, _, first, second = two_tables(build_agent, web_client)
    client.post(f"/n/{nb}/cells/{c1}/delete", data={"tables": "delete_rt"})
    assert not readable(client, first) and readable(client, second)


def test_ownership_is_the_recorded_run_not_the_first_mention(build_agent, web_client):
    # cell one made rt_X and then used `kept`, a table cell two made and that was renamed since
    agent = build_agent(ScriptedLLM([TextChunk("hi")], [TextChunk("hi")]))
    client = web_client(agent)
    nb, one, _ = ask(client, "one")
    _, two, _ = ask(client, "two", nb)
    run_one, run_two = (agent.notebooks.cell(c)["run"]["id"] for c in (one, two))
    agent.tables.register_for(CellRun(one, run_one), "rt_20260101_000000")
    agent.tables.register_for(CellRun(two, run_two), "kept")
    agent.notebooks.add_message(run_one, {"role": "assistant", "content": "first used kept here"})
    assert client.get(f"/n/{nb}/cells/{one}/delete-preview").json() == {"result": 1, "saved": 0}
    assert client.get(f"/n/{nb}/cells/{two}/delete-preview").json() == {"result": 0, "saved": 1}


def test_dialog_markup_offers_keep_or_delete_result_tables(build_agent, web_client):
    client, _, nb, _, _, _, _ = two_tables(build_agent, web_client)
    page = client.get(f"/n/{nb}").text
    assert 'id="delete-dialog"' in page
    for value in ("keep", "delete_rt"):
        assert f'value="{value}"' in page
    assert 'value="delete_all"' not in page
    assert f'data-preview-url="/n/{nb}/delete-preview"' in client.get("/").text
