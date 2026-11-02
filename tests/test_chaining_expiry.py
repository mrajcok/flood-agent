"""Result Table chaining across turns and expiry, through the Agent turn seam and thin HTTP layer."""

import re

import pytest
from conftest import ScriptedLLM, all_telemetry_values, collect, last_tool_result, query_limits, tool_call

from flood_agent.agent import Conversation, DisplayEvent
from flood_agent.llm import TextChunk

BY_PORT = "SELECT dport, SUM(bytes) AS total_bytes FROM flows GROUP BY dport"
HOUR = 3_600_000


class Clock:
    def __init__(self, ms=1_000_000):
        self.ms = ms

    def __call__(self):
        return self.ms


async def run_first(make_agent, request="bytes per port please", **cfg):
    """Turn 1: run_query only. Returns (agent, llm, conversation, result table name)."""
    llm = ScriptedLLM([tool_call("run_query", sql=BY_PORT, title="Bytes per port")], [TextChunk("done")])
    agent = await make_agent(llm, **cfg)
    conv = Conversation()
    await collect(agent, request, conv)
    return agent, llm, conv, last_tool_result(llm.calls[1])["result_table"]


async def test_follow_up_prompt_lists_result_table_schema_title_and_request(make_agent):
    agent, llm, conv, name = await run_first(make_agent)
    llm.steps = [[TextChunk("ok")]]
    await collect(agent, "now break that down", conv)
    system = llm.calls[2][0]["content"]
    assert name in system and "Bytes per port" in system
    assert "bytes per port please" in system
    assert "dport" in system and "total_bytes" in system


async def test_follow_up_query_can_select_from_previous_result_table(make_agent):
    agent, llm, conv, name = await run_first(make_agent)
    llm.steps = [
        [tool_call("run_query", sql=f"SELECT dport FROM {name} WHERE total_bytes > 600000", title="big")],
        lambda msgs: [tool_call("show_table", result_table=last_tool_result(msgs)["result_table"])],
        [TextChunk("ok")],
    ]
    events = await collect(agent, "only the big ones", conv)
    d = next(e for e in events if isinstance(e, DisplayEvent))
    assert agent.fetch_rows(d.result_table)["rows"] == [[8443]]


async def test_other_conversations_result_tables_are_invisible_and_unqueryable(make_agent):
    agent, llm, conv, name = await run_first(make_agent)
    llm.steps = [
        [tool_call("run_query", sql=f"SELECT * FROM {name}", title="peek")],
        [TextChunk("failed")],
    ]
    await collect(agent, "other conversation question", Conversation())
    assert name not in llm.calls[2][0]["content"]
    result = last_tool_result(llm.calls[3])
    assert result["success"] is False
    assert name not in str(result)


async def test_chained_turns_never_leak_telemetry_values(make_agent, telemetry_db):
    agent, llm, conv, name = await run_first(make_agent)
    llm.steps = [
        [tool_call("run_query", sql=f"SELECT * FROM {name}", title="again")],
        lambda msgs: [tool_call("show_table", result_table=last_tool_result(msgs)["result_table"])],
        [TextChunk("shown")],
    ]
    await collect(agent, "show it again", conv)
    values = {v for v in all_telemetry_values(telemetry_db) if len(v) >= 4} | {"1111111", "555321"}
    for call in llm.calls:
        for m in call:
            if m["role"] in ("system", "tool"):
                for v in values:
                    assert v not in m["content"], (v, m["role"])


async def test_result_tables_expire_after_ttl_of_inactivity(make_agent):
    clock = Clock()
    agent, llm, conv, name = await run_first(make_agent, clock=clock, result_ttl_ms=HOUR)
    clock.ms += HOUR - 1
    assert agent.fetch_rows(name)["rows"]  # still live
    llm.steps = [[TextChunk("hi")]]
    await collect(agent, "keep alive", conv)  # activity resets the timer
    clock.ms += HOUR - 1
    assert agent.fetch_rows(name)["rows"]
    clock.ms += 2
    with pytest.raises(Exception) as exc:
        agent.fetch_rows(name)
    assert "expired" in str(exc.value).lower()


async def test_expired_table_is_gone_from_prompt_and_unqueryable(make_agent):
    clock = Clock()
    agent, llm, conv, name = await run_first(make_agent, clock=clock, result_ttl_ms=HOUR)
    clock.ms += HOUR + 1
    llm.steps = [
        [tool_call("run_query", sql=f"SELECT * FROM {name}", title="late")],
        [TextChunk("sorry")],
    ]
    await collect(agent, "use the old one", conv)
    assert name not in llm.calls[2][0]["content"]
    assert last_tool_result(llm.calls[3])["success"] is False


def chat(client, message, conversation_id=""):
    posted = client.post("/chat", data={"message": message, "conversation_id": conversation_id, "max_rows": "1000"})
    cid = re.search(r'name="conversation_id" value="([0-9a-f]+)"', posted.text).group(1)
    body = client.get(posted.text.split('sse-connect="')[1].split('"')[0]).text
    return cid, body


def test_expired_display_renders_expired_and_rows_endpoint_gives_410(build_agent, web_client):
    clock = Clock()
    llm = ScriptedLLM(
        [tool_call("run_query", sql=BY_PORT, title="t")],
        lambda msgs: [tool_call("show_table", result_table=last_tool_result(msgs)["result_table"])],
        [TextChunk("here")],
    )
    client = web_client(build_agent(llm, clock=clock, result_ttl_ms=HOUR))
    cid, body = chat(client, "ports")
    rows_url = body.split('data-rows-url="')[1].split('"')[0]
    assert client.get(rows_url).status_code == 200

    clock.ms += HOUR + 1
    resp = client.get(rows_url)
    assert resp.status_code == 410 and "expired" in resp.json()["detail"].lower()

    for suffix in ("psv", "chart-rows"):
        gone = client.get(rows_url.replace("/rows", "/" + suffix))
        assert gone.status_code == 410 and "expired" in gone.json()["detail"].lower()

    page = client.get(f"/c/{cid}").text.split("</aside>")[1]
    assert "expired" in page.lower() and "data-rows-url" not in page


async def test_same_turn_chain_selects_from_table_created_earlier_in_the_turn(make_agent):
    llm = ScriptedLLM(
        [tool_call("run_query", sql=BY_PORT, title="Bytes per port")],
        lambda msgs: [
            tool_call(
                "run_query",
                sql=f"SELECT dport FROM {last_tool_result(msgs)['result_table']} WHERE total_bytes > 600000",
                title="big",
            )
        ],
        lambda msgs: [tool_call("show_table", result_table=last_tool_result(msgs)["result_table"])],
        [TextChunk("ok")],
    )
    agent = await make_agent(llm)
    events = await collect(agent, "big ports", Conversation())
    d = next(e for e in events if isinstance(e, DisplayEvent))
    assert agent.fetch_rows(d.result_table)["rows"] == [[8443]]


def test_single_composed_authorizer_enforces_action_list_and_table_scope(telemetry_db, tmp_path, monkeypatch):
    """With the SELECT guard patched out, the one authorizer still rejects DDL/DML/ATTACH/PRAGMA
    and reads of Result Tables outside the allowed set, while the CTAS and own-table reads work."""
    import sqlite3

    from flood_agent import queries

    results = tmp_path / "results.db"
    monkeypatch.setattr(queries, "validate_select", lambda sql: sql)
    first = queries.run_query(telemetry_db, results, "SELECT dport FROM flows", **query_limits(), allowed_tables=set())
    assert first["success"] is True  # CTAS into results still works
    own = first["result_table"]
    other = queries.run_query(telemetry_db, results, "SELECT dport FROM flows", **query_limits(), allowed_tables=set())["result_table"]
    assert queries.run_query(telemetry_db, results, f"SELECT * FROM {own}", **query_limits(), allowed_tables={own})["success"]
    assert not queries.run_query(telemetry_db, results, f"SELECT * FROM {other}", **query_limits(), allowed_tables={own})["success"]
    for sql in [
        "SELECT 1) ; DROP TABLE flows; --",
        "SELECT 1) ; ATTACH DATABASE ':memory:' AS e; --",
        "SELECT 1) ; PRAGMA writable_schema=1; --",
        f"SELECT 1) ; DROP TABLE results.{own}; --",
        "SELECT 1) ; CREATE TABLE main.evil(a); --",
    ]:
        assert queries.run_query(telemetry_db, results, sql, **query_limits(), allowed_tables={own})["success"] is False
    con = sqlite3.connect(results)
    assert {r[0] for r in con.execute("SELECT name FROM sqlite_master")} >= {own, other}
