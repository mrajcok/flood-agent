"""Hallucinated-value flagging: unseen IP-like / large-number tokens in the reply are flagged
for the analyst (reply note + Trace `flag` entries), and never sent to the LLM."""

import json
import re

import pytest
from conftest import ScriptedLLM, collect, last_tool_result, tool_call

from flood_agent.agent import Conversation, FlagEvent, TraceReady
from flood_agent.flagging import find_unseen_values
from flood_agent.llm import TextChunk

SQL = "SELECT dport, SUM(bytes) AS total_bytes FROM flows GROUP BY dport ORDER BY total_bytes DESC"


def flags_of(events):
    return [t for e in events if isinstance(e, FlagEvent) for t in e.tokens]


def trace_flags(events):
    (tr,) = [e.trace for e in events if isinstance(e, TraceReady)]
    return [e for e in tr["entries"] if e["kind"] == "flag"]


async def test_invented_ip_and_large_number_are_flagged(make_agent):
    llm = ScriptedLLM([TextChunk("The top talker is 203.0.113.99 with 4.2M packets.")])
    events = await collect(await make_agent(llm), "who is the top talker?")
    assert sorted(flags_of(events)) == ["203.0.113.99", "4.2M"]
    entries = trace_flags(events)
    assert {(e["type"], e["token"]) for e in entries} == {("ip", "203.0.113.99"), ("number", "4.2M")}
    # the flag note comes after the reply text
    kinds = [type(e).__name__ for e in events]
    assert kinds.index("FlagEvent") > max(i for i, k in enumerate(kinds) if k == "TextDelta")


async def test_values_the_llm_legitimately_knows_are_not_flagged(make_agent):
    llm = ScriptedLLM(
        [tool_call("run_query", sql=SQL + " LIMIT 1000000", title="t")],
        lambda msgs: [TextChunk(
            "You asked about 198.51.100.7 and 3 Gbps. "  # user message
            "Example source 192.0.2.1, window_end 1600000100000, window 1,600,000,100,000. "  # Semantic Layer
            f"The query returned {last_tool_result(msgs)['row_count']} rows; "  # Query Metadata
            "I filtered on etime < 1600000100000 and limit 1,000,000."  # own SQL
        )],
    )
    events = await collect(await make_agent(llm), "Look at 198.51.100.7 around 3 Gbps")
    assert flags_of(events) == []
    assert trace_flags(events) == []


async def test_row_count_in_query_metadata_is_not_flagged(make_agent):
    # 3 rows is small; use a threshold-crossing count via the metadata path in the table test below.
    llm = ScriptedLLM(
        [tool_call("run_query", sql="SELECT * FROM flows", title="t")],
        lambda msgs: [TextChunk(f"Got {last_tool_result(msgs)['row_count']} rows.")],
    )
    events = await collect(await make_agent(llm), "show flows")
    assert flags_of(events) == []


async def test_assistants_own_earlier_prose_does_not_whitelist_a_value(make_agent):
    agent = await make_agent(ScriptedLLM([TextChunk("It was 203.0.113.99.")], [TextChunk("Again 203.0.113.99.")]))
    conv = Conversation()
    first = await collect(agent, "q1", conv)
    second = await collect(agent, "q2", conv)
    assert flags_of(first) == ["203.0.113.99"] and flags_of(second) == ["203.0.113.99"]


async def test_flags_never_reach_the_llm(make_agent):
    llm = ScriptedLLM([TextChunk("Host 203.0.113.99 sent 4.2M packets.")], [TextChunk("fine")])
    agent = await make_agent(llm)
    conv = Conversation()
    await collect(agent, "first", conv)
    await collect(agent, "second", agent.open_conversation(conv.id))
    # Everything except the LLM's own earlier prose must be flag-free.
    sent = json.dumps([[m for m in call if m["role"] != "assistant"] for call in llm.calls])
    assert "Unverified values" not in sent and "203.0.113.99" not in sent and "4.2M" not in sent
    assert '"flag"' not in sent and "token_type" not in sent
    assert "flag-note" not in json.dumps(llm.calls) and "Unverified values" not in json.dumps(llm.calls)


def ask(client, message, conversation_id=""):
    posted = client.post("/chat", data={"message": message, "conversation_id": conversation_id, "max_rows": "1000"})
    cid = re.search(r'name="conversation_id" value="([0-9a-f]+)"', posted.text).group(1)
    stream_url = posted.text.split('sse-connect="')[1].split('"')[0]
    return cid, client.get(stream_url).text


def test_flag_note_streams_and_survives_restart(build_agent, web_client):
    llm = ScriptedLLM([TextChunk("Host 203.0.113.99 sent 4.2M <b>packets</b>.")])
    client = web_client(build_agent(llm))
    cid, body = ask(client, "q")
    assert "event: flag" in body
    flag_part = body.split("event: flag")[1].split("event: ")[0]
    assert "203.0.113.99" in flag_part and "4.2M" in flag_part and "<b>" not in flag_part

    restarted = web_client(build_agent(ScriptedLLM()))
    page = restarted.get(f"/c/{cid}").text
    assert 'class="flag-note"' in page and "203.0.113.99" in page and "4.2M" in page
    assert page.index('class="flag-note"') < page.index('class="trace"')
    assert "&lt;b&gt;packets&lt;/b&gt;" in page


def test_unflagged_turn_has_no_flag_note(build_agent, web_client):
    client = web_client(build_agent(ScriptedLLM([TextChunk("Nothing numeric here, 42 rows.")])))
    cid, body = ask(client, "q")
    assert "event: flag" not in body
    assert 'class="flag-note"' not in client.get(f"/c/{cid}").text


def test_trace_renders_flag_entries_readably(build_agent, web_client):
    client = web_client(build_agent(ScriptedLLM([TextChunk("9.9.9.9")])))
    cid, _ = ask(client, "q")
    page = client.get(f"/c/{cid}").text
    assert 'class="trace-entry trace-flag"' in page and "Flagged IP address" in page


# Heuristic table against a fixed allowed context.
CTX = ["User asked about 10.1.2.3 and port 8443; window_end 1600000100000; 12,345 rows"]


@pytest.mark.parametrize(
    "reply, expected",
    [
        ("see 10.1.2.3", []),  # IP from context
        ("see 10.1.2.4", ["10.1.2.4"]),  # invented IP
        ("see 10.1.2.4/24", ["10.1.2.4"]),  # CIDR host part
        ("see v1.2.3.4 and 1.2.3", []),  # version-like
        ("see 300.1.2.3", []),  # not a valid octet
        ("on port 8443 and 443 and 51234", ["51234"]),  # ports below threshold ok; 5 digits is a value
        ("only 42 flows, 9,999 hosts", []),  # small counts
        ("2024 and 1999", []),  # years
        ("12,345 rows / 12345 rows", []),  # same value, different format
        ("about 12.345K rows", []),  # same value via suffix
        ("987,654 bytes", ["987,654"]),  # large invented, commas
        ("987654 bytes", ["987654"]),
        ("4.2M packets", ["4.2M"]),
        ("rate hit 3 Gbps", ["3 Gbps"]),
        ("3.5 million flows", ["3.5 million"]),
        ("1.5 billion", ["1.5 billion"]),
        ("2 TB moved", ["2 TB"]),
        ("1600000100000 is window_end", []),
        ("1700000000111", ["1700000000111"]),
        ("(987,654.5)", ["987,654.5"]),
        ("ended at 987,654.", ["987,654"]),
        ("203.0.113.99, 203.0.113.99", ["203.0.113.99"]),  # deduplicated
        ("rt_1234567 ABC12345678", []),  # digits inside identifiers
        ("a cable 20 m long", []),  # spaced single letter is prose, not a magnitude
        ("top 10 T cells and 15 G", []),
        ("20 M", []),
        ("20M packets", ["20M"]),
        ("5Gbps", ["5Gbps"]),
        ("5 Gbps", ["5 Gbps"]),
        ("1 k", []),
        ("20 million", ["20 million"]),
    ],
)
def test_heuristic_table(reply, expected):
    assert find_unseen_values(reply, CTX) == expected
