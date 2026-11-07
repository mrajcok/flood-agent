"""Full Semantic Layer (ticket #4): tested at the Agent turn seam with the scripted fake LLM."""

from pathlib import Path

import pytest
from conftest import ScriptedLLM, all_telemetry_values, collect, config_for_test, last_tool_result, tool_call

from flood_agent.agent import Agent
from flood_agent.llm import TextChunk
from flood_agent.semantic_layer import SemanticLayer

REAL = Path(__file__).parent.parent / "semantic_layer.yaml"


@pytest.fixture
async def real_layer_agent(tmp_path, telemetry_db):
    """Factory for started Agents on the real Semantic Layer; closed at teardown."""
    agents = []

    async def _make(llm, tmp_path=tmp_path, telemetry_db=telemetry_db):
        agent = Agent(config_for_test(tmp_path, telemetry_db, REAL), lambda model: llm)
        await agent.start()
        agents.append(agent)
        return agent

    yield _make
    for agent in agents:
        await agent.aclose()


async def test_system_prompt_carries_full_semantic_layer_and_no_telemetry(real_layer_agent, telemetry_db):
    llm = ScriptedLLM([TextChunk("ok")])
    agent = await real_layer_agent(llm)
    await collect(agent, "SYN packets from 10.1.0.0/16 in the last hour?")
    system = llm.calls[0][0]
    assert system["role"] == "system"
    prompt = system["content"]
    window_end = str(SemanticLayer(REAL).window_end)
    # dialect, Time Key rule, "now" = window_end, relative phrases
    assert "sqlite" in prompt and "etime" in prompt
    assert window_end in prompt and "last hour" in prompt
    # enums so names map to values
    for needle in ("TCP", "UDP", "ICMP", "SYN", "ACK", "FIN", "RST", "PSH", "URG"):
        assert needle in prompt
    # UDF documented with Ocient caveat, and common metrics
    assert "in_cidr" in prompt and "Ocient" in prompt
    assert "bits" in prompt.lower() and "top talkers" in prompt.lower()
    # invariant: no telemetry values anywhere in what the LLM was sent
    sent = llm.all_sent_text()
    # (short values like proto 6 legitimately appear as enum keys, so only check distinctive ones)
    for value in (v for v in all_telemetry_values(telemetry_db) if len(v) >= 5):
        assert value not in sent, value


async def test_in_cidr_works_in_llm_generated_query_via_mcp(make_agent):
    sql = (
        "SELECT dport, COUNT(*) AS n FROM flows "
        "WHERE in_cidr(sip, '172.30.41.0/24') AND NOT in_cidr(sip, '10.0.0.0/8') GROUP BY dport"
    )
    llm = ScriptedLLM([tool_call("run_query", sql=sql, title="cidr")], [TextChunk("done")])
    agent = await make_agent(llm)
    await collect(agent, "traffic from 172.30.41.0/24")
    meta = last_tool_result(llm.calls[1])
    assert meta["success"] is True and meta["row_count"] == 1
    assert agent.fetch_rows(meta["result_table"])["rows"] == [[8443, 2]]


def test_shipped_layer_shape_is_stable_for_table_formatting_and_generator():
    data = SemanticLayer(REAL).data
    cols = data["tables"]["flows"]["columns"]
    assert cols["proto"]["enum"] == {6: "TCP", 17: "UDP", 1: "ICMP"}
    assert cols["tcp_flags"]["enum_bits"][2] == "SYN"
    assert cols["bytes"]["unit"] == "bytes" and cols["etime"]["unit"] == "epoch ms UTC"
    assert "window_start" in data and "window_end" in data
