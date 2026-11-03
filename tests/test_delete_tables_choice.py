"""Choosing what happens to Result Tables when deleting a Conversation or one exchange (HTTP seam)."""

import re

from conftest import ScriptedLLM, last_tool_result, tool_call
from test_conversations import ask, main_pane, table_script, turn_ids

from flood_agent.llm import TextChunk
from flood_agent.web import TEMPLATES

DAY = 24 * 3_600_000


class Clock:
    def __init__(self, ms=1_000_000):
        self.ms = ms

    def __call__(self):
        return self.ms


def tables_in(client, cid):
    return re.findall(r"/api/results/([a-z0-9_]+)/rows", client.get(f"/c/{cid}").text)


def two_tables(build_agent, web_client, *extra):
    """One Conversation, two exchanges, one table each; the second table is renamed."""
    clock = Clock()
    agent = build_agent(ScriptedLLM(*table_script("a"), *table_script("b"), *extra), clock=clock, result_ttl_ms=DAY)
    client = web_client(agent)
    cid, _ = ask(client, "first")
    clock.ms += 2000  # distinct creation times
    ask(client, "second", cid)
    first, second = tables_in(client, cid)[:2]
    client.post(f"/api/results/{second}/settings", data={"title": "keeper"})
    second = "keeper"
    return client, clock, cid, first, second


def readable(client, name):
    return client.get(f"/api/results/{name}/rows").status_code == 200


def test_preview_counts_live_tables_and_those_still_named_rt(build_agent, web_client):
    client, _, cid, _, _ = two_tables(build_agent, web_client)
    assert client.get(f"/c/{cid}/delete-preview").json() == {"total": 2, "rt": 1}
    first_turn, second_turn = turn_ids(main_pane(client.get(f"/c/{cid}").text))
    assert client.get(f"/c/{cid}/delete-preview?turn_id={first_turn}").json() == {"total": 1, "rt": 1}
    assert client.get(f"/c/{cid}/delete-preview?turn_id={second_turn}").json() == {"total": 1, "rt": 0}
    assert client.get("/c/nope/delete-preview").status_code == 404
    assert client.get(f"/c/{cid}/delete-preview?turn_id=9999").status_code == 404


def test_deleting_a_conversation_keeps_its_tables_by_default_until_they_expire(build_agent, web_client):
    client, clock, cid, first, second = two_tables(build_agent, web_client)
    assert client.post(f"/c/{cid}/delete", follow_redirects=False).status_code == 303
    assert readable(client, first) and readable(client, second)
    clock.ms += DAY + 10_000
    assert client.get(f"/api/results/{first}/rows").status_code == 410


def test_a_kept_table_is_queryable_from_another_conversation(build_agent, web_client):
    seen = {}

    def query_kept(msgs):
        return [tool_call("run_query", sql=f"SELECT * FROM {seen['name']}", title="again")]

    client, _, cid, first, _ = two_tables(build_agent, web_client, query_kept, lambda msgs: [
        TextChunk("ok" if last_tool_result(msgs).get("success") else "failed")])
    seen["name"] = first
    client.post(f"/c/{cid}/delete", data={"tables": "keep"})
    _, stream = ask(client, "from elsewhere")
    assert "ok" in stream and "failed" not in stream


def test_delete_rt_drops_only_tables_still_named_rt(build_agent, web_client):
    client, _, cid, first, second = two_tables(build_agent, web_client)
    client.post(f"/c/{cid}/delete", data={"tables": "delete_rt"})
    assert not readable(client, first) and readable(client, second)


def test_delete_all_drops_every_table(build_agent, web_client):
    client, _, cid, first, second = two_tables(build_agent, web_client)
    client.post(f"/c/{cid}/delete", data={"tables": "delete_all"})
    assert not readable(client, first) and not readable(client, second)


def test_unknown_choice_is_rejected_and_deletes_nothing(build_agent, web_client):
    client, _, cid, first, _ = two_tables(build_agent, web_client)
    assert client.post(f"/c/{cid}/delete", data={"tables": "bogus"}).status_code == 422
    assert readable(client, first) and f'href="/c/{cid}"' in client.get("/").text


def test_turn_deletion_keeps_by_default_and_honours_the_choices(build_agent, web_client):
    client, _, cid, first, second = two_tables(build_agent, web_client)
    first_turn, second_turn = turn_ids(main_pane(client.get(f"/c/{cid}").text))
    client.post(f"/c/{cid}/turns/{first_turn}/delete")
    assert readable(client, first)
    client.post(f"/c/{cid}/turns/{second_turn}/delete", data={"tables": "delete_rt"})
    assert readable(client, second)  # renamed, so delete_rt keeps it


def test_turn_delete_all_drops_that_turns_tables_only(build_agent, web_client):
    client, _, cid, first, second = two_tables(build_agent, web_client)
    first_turn, _ = turn_ids(main_pane(client.get(f"/c/{cid}").text))
    client.post(f"/c/{cid}/turns/{first_turn}/delete", data={"tables": "delete_all"})
    assert not readable(client, first) and readable(client, second)


def test_turn_ownership_matches_whole_table_names(build_agent, web_client):
    # rt_X is a substring of rt_X_2: a turn mentioning only rt_X_2 must not be taken to own rt_X
    agent = build_agent(ScriptedLLM([TextChunk("hi")], [TextChunk("hi")]))
    client = web_client(agent)
    cid, _ = ask(client, "one")
    ask(client, "two", cid)
    one, two = turn_ids(main_pane(client.get(f"/c/{cid}").text))
    base, suffixed = "rt_20260101_000000", "rt_20260101_000000_2"
    agent.store.add_result_table(cid, base)
    agent.store.add_result_table(cid, suffixed)
    agent.store.add_message(cid, int(one), {"role": "assistant", "content": f"made {suffixed}"})
    agent.store.add_message(cid, int(two), {"role": "assistant", "content": f"made {base}"})
    assert client.get(f"/c/{cid}/delete-preview?turn_id={one}").json()["total"] == 1
    assert client.get(f"/c/{cid}/delete-preview?turn_id={two}").json()["total"] == 1


def test_dialog_markup_offers_the_three_choices(build_agent, web_client):
    client, _, cid, _, _ = two_tables(build_agent, web_client)
    page = client.get(f"/c/{cid}").text
    assert 'id="delete-dialog"' in page
    for value in ("keep", "delete_rt", "delete_all"):
        assert f'value="{value}"' in page
    assert f'data-preview-url="/c/{cid}/delete-preview"' in page
    assert "delete-preview?turn_id=" in TEMPLATES.get_template("turn_delete.html").render(conversation_id=cid, turn_id=3)
