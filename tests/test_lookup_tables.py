"""Adding a Lookup Table from pasted rows (plain HTTP, never MCP), then using it in turns."""

import json
import re
import sqlite3

import pytest
from conftest import ScriptedLLM, all_telemetry_values, last_tool_result, tool_call

from flood_agent.llm import TextChunk, ToolCallChunk

DAY = 24 * 3_600_000
SECRET = "ZQ-LOOKUP-SECRET-4471"


class Clock:
    def __init__(self, ms=1_000_000):
        self.ms = ms

    def __call__(self):
        return self.ms


def add(client, name, rows, overwrite=False):
    data = {"name": name, "rows": rows}
    if overwrite:
        data["overwrite"] = "on"
    return client.post("/api/lookup-tables", data=data)


def run_turn(client, message="hello"):
    posted = client.post("/chat", data={"message": message, "conversation_id": "", "max_rows": "1000"})
    return client.get(re.search(r'sse-connect="([^"]+)"', posted.text).group(1)).text


def describe(name, call_id="c1"):
    return ToolCallChunk(id=call_id, name="describe_table", arguments=json.dumps({"name": name}))


def stored(agent, table):
    """Rows of a Lookup Table, read straight from the results database (its cells never reach the LLM)."""
    con = sqlite3.connect(agent.config.results_db)
    try:
        return [list(r) for r in con.execute(f'SELECT * FROM "{table}" ORDER BY rowid')]
    finally:
        con.close()


@pytest.mark.parametrize(
    "paste",
    [
        "ip\tdesc\n10.0.0.1\tgateway\n10.0.0.2\tdns",
        "ip|desc\n10.0.0.1|gateway\n10.0.0.2|dns",
        "ip,desc\n10.0.0.1,gateway\n10.0.0.2,dns",
    ],
)
def test_tab_pipe_and_comma_pastes_create_the_table(build_agent, web_client, paste):
    agent = build_agent(ScriptedLLM())
    r = add(web_client(agent), "hosts", paste)
    assert r.status_code == 200
    assert r.json() == {"name": "lookup_hosts", "row_count": 2, "columns": ["ip", "desc"]}
    assert stored(agent, "lookup_hosts") == [["10.0.0.1", "gateway"], ["10.0.0.2", "dns"]]


def test_quoted_cells_may_contain_the_separator(build_agent, web_client):
    agent = build_agent(ScriptedLLM())
    client = web_client(agent)
    assert add(client, "q", 'ip,desc\n10.0.0.1,"edge, east"\n10.0.0.2,"say ""hi"""').status_code == 200
    assert stored(agent, "lookup_q") == [["10.0.0.1", "edge, east"], ["10.0.0.2", 'say "hi"']]
    assert add(client, "p", 'ip|desc\n10.0.0.3|"a|b"').status_code == 200
    assert stored(agent, "lookup_p") == [["10.0.0.3", "a|b"]]


def test_tab_wins_over_pipe_which_wins_over_comma_by_the_header_line(build_agent, web_client):
    client = web_client(build_agent(ScriptedLLM()))
    assert add(client, "mix", "a\tb\n1|2\t3").json()["row_count"] == 1
    assert add(client, "mix2", "a|b\n1,2|3").json()["row_count"] == 1


def test_single_column_blank_lines_trimming_and_nulls(build_agent, web_client):
    agent = build_agent(ScriptedLLM())
    client = web_client(agent)
    r = add(client, "one", "  ip \n\n 10.0.0.1 \n   \n10.0.0.2\n")
    assert r.json() == {"name": "lookup_one", "row_count": 2, "columns": ["ip"]}
    assert stored(agent, "lookup_one") == [["10.0.0.1"], ["10.0.0.2"]]
    assert add(client, "nulls", "a,b,c\n1,,x\n , 2 ,").json()["row_count"] == 2
    assert stored(agent, "lookup_nulls") == [["1", None, "x"], [None, "2", None]]


def test_all_columns_are_text(build_agent, web_client):
    agent = build_agent(ScriptedLLM())
    add(web_client(agent), "t", "n\n007")
    assert stored(agent, "lookup_t") == [["007"]]


@pytest.mark.parametrize("bad", ["", "a b", "x;drop", 'x"y', "a" * 100])
def test_unsafe_names_are_rejected(build_agent, web_client, bad):
    assert add(web_client(build_agent(ScriptedLLM())), bad, "a\n1").status_code == 422


def test_header_only_or_empty_paste_is_rejected(build_agent, web_client):
    client = web_client(build_agent(ScriptedLLM()))
    assert add(client, "e", "").status_code == 422
    assert add(client, "e", "a,b").status_code == 422


def test_any_conversation_can_describe_and_join_against_it(build_agent, web_client, telemetry_db):
    llm = ScriptedLLM(
        [describe("lookup_hosts")],
        lambda msgs: [tool_call(
            "run_query", title="joined",
            sql="SELECT f.dport, h.desc FROM flows f JOIN lookup_hosts h ON h.ip = f.dip",
        )],
        [TextChunk("done")],
    )
    agent = build_agent(llm)
    client = web_client(agent)
    assert add(client, "hosts", f"ip,desc\n198.18.77.200,{SECRET}").status_code == 200
    # Creating it wrote nothing to any Conversation.
    assert agent.store.list_conversations() == []
    run_turn(client)
    described = last_tool_result(llm.calls[1])
    assert described["success"] is True and described["kind"] == "lookup"
    assert [c["name"] for c in described["columns"]] == ["ip", "desc"]
    assert described["row_count"] == 1
    joined = last_tool_result(llm.calls[2])
    assert joined["success"] is True and joined["row_count"] == 2
    # Nothing the LLM saw carries a cell value (the leak invariant, extended to Lookup Table cells).
    sent = llm.all_sent_text()
    assert not any(v in sent for v in all_telemetry_values(telemetry_db) | {SECRET} if len(v) >= 8)


def test_the_next_turn_does_not_mention_the_lookup_table(build_agent, web_client):
    llm = ScriptedLLM([TextChunk("hi")])
    agent = build_agent(llm)
    client = web_client(agent)
    add(client, "hosts", f"ip,desc\n198.18.77.200,{SECRET}")
    run_turn(client)
    sent = llm.all_sent_text()
    assert "lookup_hosts" not in sent and SECRET not in sent


def test_a_result_table_named_lookup_is_not_a_lookup_table(build_agent, web_client):
    llm = ScriptedLLM(
        [tool_call("run_query", sql="SELECT dport FROM flows", title="lookup_fake")],
        lambda msgs: [describe(last_tool_result(msgs)["result_table"], "c2")],
        [TextChunk("ok")],
    )
    agent = build_agent(llm)
    run_turn(web_client(agent))
    assert last_tool_result(llm.calls[2])["kind"] == "result"


def test_a_lookup_table_never_expires(build_agent, web_client):
    clock = Clock()
    llm = ScriptedLLM([describe("lookup_hosts")], [TextChunk("ok")])
    agent = build_agent(llm, clock=clock, result_ttl_ms=DAY)
    client = web_client(agent)
    add(client, "hosts", "ip\n1.1.1.1")
    clock.ms += 1000 * DAY
    agent.sweep_expired()
    run_turn(client)
    assert last_tool_result(llm.calls[1])["kind"] == "lookup"
    assert stored(agent, "lookup_hosts") == [["1.1.1.1"]]


def test_sidebar_has_the_add_lookup_table_dialog(build_agent, web_client):
    html = web_client(build_agent(ScriptedLLM())).get("/").text
    assert "Add lookup table" in html
    assert "First row must be the column names. Separate columns with tab, comma or `|`." in html
    assert "Overwrite existing table" in html
    assert "/api/lookup-tables" in html


def rejected(client, agent, name, paste, message, table="lookup_t", **kw):
    """The submit is refused with a message containing `message`, and `table` was not created."""
    r = add(client, name, paste, **kw)
    assert r.status_code == 422
    assert message in r.json()["detail"]
    con = sqlite3.connect(agent.config.results_db)
    try:
        found = con.execute("SELECT 1 FROM sqlite_master WHERE name=?", (table,)).fetchone()
    except sqlite3.Error:
        found = None
    finally:
        con.close()
    assert found is None
    assert table not in agent.store.lookup_table_names()


def test_bad_table_name_is_named_in_the_message(build_agent, web_client):
    agent = build_agent(ScriptedLLM())
    client = web_client(agent)
    rejected(client, agent, "9lives", "a\n1", "9lives", table="lookup_9lives")
    rejected(client, agent, "a b", "a\n1", "a b", table="lookup_a b")
    rejected(client, agent, "x" * 61, "a\n1", "60", table="lookup_" + "x" * 61)


@pytest.mark.parametrize("bad", ["1a", "a b", 'a"b', "a-b", "x" * 61])
def test_bad_column_name_is_named_in_the_message(build_agent, web_client, bad):
    agent = build_agent(ScriptedLLM())
    rejected(web_client(agent), agent, "t", f"ok,{bad}\n1,2" if "," not in bad else "", bad)


def test_repeated_column_names_ignoring_case_are_rejected(build_agent, web_client):
    agent = build_agent(ScriptedLLM())
    rejected(web_client(agent), agent, "t", "ip,IP\n1,2", "ip")


def test_rows_with_the_wrong_cell_count_give_line_numbers(build_agent, web_client):
    agent = build_agent(ScriptedLLM())
    paste = "a,b\n1,2\n3\n4,5\n6,7,8"
    client = web_client(agent)
    rejected(client, agent, "t", paste, "line 3")
    r = add(client, "t", paste)
    assert "3, 5" in r.json()["detail"]


def test_wrong_cell_count_lists_only_the_first_few_lines(build_agent, web_client):
    agent = build_agent(ScriptedLLM())
    paste = "a,b\n" + "\n".join("x" for _ in range(20))
    detail = add(web_client(agent), "t", paste).json()["detail"]
    assert "line 2" in detail and "line 21" not in detail


def test_repeated_first_column_values_give_values_and_lines(build_agent, web_client):
    agent = build_agent(ScriptedLLM())
    paste = "ip,desc\n10.0.0.1,a\n10.0.0.2,b\n 10.0.0.1 ,c"
    client = web_client(agent)
    rejected(client, agent, "t", paste, "10.0.0.1")
    detail = add(client, "t", paste).json()["detail"]
    assert "lines 2, 4" in detail and "10.0.0.2" not in detail


def test_first_column_duplicates_are_case_sensitive_and_other_columns_may_repeat(build_agent, web_client):
    client = web_client(build_agent(ScriptedLLM()))
    assert add(client, "t", "k,v\nA,1\na,1").status_code == 200


def test_header_without_data_rows_is_rejected(build_agent, web_client):
    agent = build_agent(ScriptedLLM())
    rejected(web_client(agent), agent, "t", "a,b\n\n  \n", "row")


def test_more_than_100000_rows_is_rejected_but_exactly_100000_is_fine(build_agent, web_client):
    agent = build_agent(ScriptedLLM())
    client = web_client(agent)
    rejected(client, agent, "t", "a\n" + "\n".join(str(i) for i in range(100_001)), "100,000")
    assert add(client, "t", "a\n" + "\n".join(str(i) for i in range(100_000))).json()["row_count"] == 100_000


def test_existing_lookup_name_is_refused_without_overwrite(build_agent, web_client):
    agent = build_agent(ScriptedLLM())
    client = web_client(agent)
    add(client, "hosts", "ip\n1.1.1.1")
    r = add(client, "hosts", "ip\n2.2.2.2")
    assert r.status_code == 422 and "lookup_hosts" in r.json()["detail"]
    assert stored(agent, "lookup_hosts") == [["1.1.1.1"]]


def test_overwrite_replaces_rows_and_columns(build_agent, web_client):
    agent = build_agent(ScriptedLLM())
    client = web_client(agent)
    add(client, "hosts", "ip\n1.1.1.1")
    r = add(client, "hosts", "name,site\nx,y\nz,w", overwrite=True)
    assert r.status_code == 200
    assert r.json() == {"name": "lookup_hosts", "row_count": 2, "columns": ["name", "site"]}
    assert stored(agent, "lookup_hosts") == [["x", "y"], ["z", "w"]]
    assert agent.store.lookup_table_names() == {"lookup_hosts"}


def test_failed_overwrite_leaves_the_old_table_queryable(build_agent, web_client):
    agent = build_agent(ScriptedLLM())
    client = web_client(agent)
    add(client, "hosts", "ip,d\n1.1.1.1,a")
    for bad in ["ip,d\n1,2\n1,3", "ip,d", "ip,d\n1,2,3", "1x,d\n1,2"]:
        assert add(client, "hosts", bad, overwrite=True).status_code == 422
    assert stored(agent, "lookup_hosts") == [["1.1.1.1", "a"]]


def test_overwrite_cannot_replace_a_result_table_or_other_table(build_agent, web_client):
    llm = ScriptedLLM([tool_call("run_query", sql="SELECT dport FROM flows", title="lookup_fake")], [TextChunk("ok")])
    agent = build_agent(llm)
    client = web_client(agent)
    run_turn(client)
    con = sqlite3.connect(agent.config.results_db)
    names = [r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")]
    con.execute("CREATE TABLE lookup_other (x TEXT)")
    con.execute("INSERT INTO lookup_other VALUES ('keep')")
    con.commit()
    con.close()
    assert names
    for target in names:
        suffix = target[len("lookup_"):] if target.startswith("lookup_") else None
        if suffix:
            r = add(client, suffix, "x\n1", overwrite=True)
            assert r.status_code == 422
    r = add(client, "other", "x\n1", overwrite=True)
    assert r.status_code == 422
    assert stored(agent, "lookup_other") == [["keep"]]
    assert "lookup_other" not in agent.store.lookup_table_names()


def test_dialog_shows_the_error_above_the_pasted_text_and_keeps_it(build_agent, web_client):
    html = web_client(build_agent(ScriptedLLM())).get("/").text
    assert html.index('class="error"') < html.index('aria-label="Rows"')
