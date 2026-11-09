"""Refresh a Count Display: POST /api/displays/{id}/refresh runs its stored count query again, without the
LLM, and overwrites the number and its "as of" time (HTTP seam, ADR 0006)."""

import re
import sqlite3
import threading
from contextlib import closing
from dataclasses import replace

from conftest import Clock, ScriptedLLM, tool_call

from flood_agent import queries
from flood_agent.llm import TextChunk

HOUR = 3_600_000
RECENT_COUNT = "SELECT COUNT(*) FROM flows WHERE etime > now() - 1500"  # 2 at 1700000003000, 1 at ...3600
SLOW = "SELECT COUNT(*) FROM " + ", ".join(f"flows t{n}" for n in range(18))


def ask(build_agent, web_client, sql=RECENT_COUNT):
    """A turn that runs one count query. Returns (client, agent, clock, llm, conversation id, display id)."""
    clock = Clock()
    llm = ScriptedLLM([tool_call("run_count_query", sql=sql, title="Recent flows")], [TextChunk("Two flows.")])
    agent = build_agent(llm, clock=clock)
    client = web_client(agent)
    posted = client.post("/chat", data={"message": "go", "conversation_id": "", "max_rows": "1000"})
    client.get(posted.text.split('sse-connect="')[1].split('"')[0])
    (cid,) = [c["id"] for c in agent.store.list_conversations()]
    display_id = int(re.search(r'data-display-id="(\d+)"', client.get(f"/c/{cid}").text).group(1))
    return client, agent, clock, llm, cid, display_id


def refresh(client, display_id):
    return client.post(f"/api/displays/{display_id}/refresh")


def shown(client, cid):
    page = client.get(f"/c/{cid}").text
    value = re.search(r'class="count-value">([\d,]+)<', page).group(1)
    return int(value.replace(",", "")), int(re.search(r'data-as-of-ms="(\d+)"', page).group(1))


def set_stored_sql(agent, sql):
    with closing(sqlite3.connect(agent.store.path)) as con, con:
        con.execute("UPDATE displays SET definition=json_set(definition, '$.sql', ?)", (sql,))


def test_refresh_gives_a_new_number_and_a_new_as_of_time_that_survive_a_reload(build_agent, web_client, monkeypatch):
    monkeypatch.setenv("FIXED_NOW_MS", "1700000003000")
    client, _, clock, _, cid, display_id = ask(build_agent, web_client)
    assert shown(client, cid) == (2, clock.ms)

    monkeypatch.setenv("FIXED_NOW_MS", "1700000003600")
    clock.ms += HOUR
    r = refresh(client, display_id)

    assert r.status_code == 200
    assert r.json() == {"value": 1, "computed_at_ms": clock.ms}
    assert shown(client, cid) == (1, clock.ms)


def test_a_query_that_times_out_fails_plainly_and_keeps_the_old_number(build_agent, web_client, monkeypatch):
    monkeypatch.setenv("FIXED_NOW_MS", "1700000003000")
    client, agent, clock, _, cid, display_id = ask(build_agent, web_client)
    before = shown(client, cid)
    set_stored_sql(agent, SLOW)
    agent.config = replace(agent.config, query_timeout_s=0.01)
    clock.ms += HOUR

    r = refresh(client, display_id)

    assert r.status_code == 422 and "time limit" in r.json()["detail"]
    assert shown(client, cid) == before


def test_the_stored_query_goes_through_the_same_one_number_and_safety_checks(build_agent, web_client):
    client, agent, clock, _, cid, display_id = ask(build_agent, web_client)
    before = shown(client, cid)
    clock.ms += HOUR
    for bad in ("DROP TABLE flows", "SELECT sip FROM flows", "SELECT COUNT(*) FROM nope",
                "SELECT COUNT(*) FROM flows GROUP BY sip"):
        set_stored_sql(agent, bad)
        assert refresh(client, display_id).status_code == 422, bad
        assert shown(client, cid) == before


def test_a_second_refresh_while_one_runs_is_a_conflict(build_agent, web_client, monkeypatch):
    client, _, _, _, _, display_id = ask(build_agent, web_client)
    started, release, real = threading.Event(), threading.Event(), queries.run_count_query

    def slow(*args, **kwargs):
        started.set()
        assert release.wait(5)
        return real(*args, **kwargs)

    monkeypatch.setattr(queries, "run_count_query", slow)
    first = []
    t = threading.Thread(target=lambda: first.append(refresh(client, display_id)))
    t.start()
    assert started.wait(5)
    try:
        assert refresh(client, display_id).status_code == 409
    finally:
        release.set()
        t.join(5)
    assert first[0].status_code == 200
    monkeypatch.setattr(queries, "run_count_query", real)
    assert refresh(client, display_id).status_code == 200  # the guard is released


def test_unknown_displays_are_not_found(build_agent, web_client):
    client, *_ = ask(build_agent, web_client)
    assert refresh(client, 9999).status_code == 404


def test_the_llm_is_told_nothing_and_its_earlier_text_is_left_alone(build_agent, web_client, monkeypatch):
    monkeypatch.setenv("FIXED_NOW_MS", "1700000003000")
    client, agent, clock, llm, cid, display_id = ask(build_agent, web_client)
    calls, messages = len(llm.calls), agent.store.messages(cid)

    monkeypatch.setenv("FIXED_NOW_MS", "1700000003600")
    clock.ms += HOUR
    refresh(client, display_id)

    assert len(llm.calls) == calls and agent.store.messages(cid) == messages
