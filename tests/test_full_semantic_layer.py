"""Full Semantic Layer (ticket #4): tested at the Agent turn seam with the scripted fake LLM."""

import sqlite3
from pathlib import Path

import pytest
from conftest import ScriptedLLM, all_telemetry_values, collect, config_for_test, last_tool_result, tool_call

from flood_agent.agent import Agent
from flood_agent.generator import END_MS, START_MS
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
    # dialect, Time Key rule, now() for relative phrases
    assert "sqlite" in prompt and "etime" in prompt
    assert "now()" in prompt and "last hour" in prompt
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
    assert cols["proto"]["enum"] == {6: "TCP", 17: "UDP", 1: "ICMP", 132: "SCTP"}
    assert cols["tcp_flags"]["enum_bits"][2] == "SYN"
    assert cols["bytes"]["unit"] == "bytes" and cols["etime"]["unit"] == "epoch ms UTC"
    assert "window_start" not in data and "window_end" not in data


def test_diameter_is_a_telemetry_table_with_named_codes(generated_db):
    layer = SemanticLayer(REAL)  # loading it runs the startup checks
    assert layer.table_names() == ["flows", "dns_metadata", "diameter"]
    assert layer.time_key("diameter") == "etime"
    con = sqlite3.connect(generated_db)
    in_database = [r[1] for r in con.execute("PRAGMA table_info(diameter)")]
    assert list(layer.data["tables"]["diameter"]["columns"]) == in_database
    for column in ("application_id", "command_code", "result_code", "experimental_result_code", "rat_type",
                   "cancellation_type", "proto"):
        generated = {v for (v,) in con.execute(f"SELECT DISTINCT {column} FROM diameter") if v is not None}
        assert generated <= set(layer.data["tables"]["diameter"]["columns"][column]["enum"]), column
    con.close()
    fmt = layer.column_formats(["application_id", "command_code", "result_code", "experimental_result_code",
                                "rat_type", "cancellation_type", "proto", "stime", "status"])
    assert fmt["application_id"]["map"] == {"16777251": "S6a", "16777238": "Gx", "4": "Gy"}
    assert fmt["command_code"]["map"]["317"] == "CLR" and fmt["command_code"]["map"]["272"] == "CCR"
    assert fmt["result_code"]["map"]["5001"] == "DIAMETER_AVP_UNSUPPORTED"
    assert fmt["experimental_result_code"]["map"]["5001"] == "DIAMETER_ERROR_USER_UNKNOWN"
    assert fmt["rat_type"]["map"]["1006"] == "NR"
    assert fmt["cancellation_type"]["map"]["2"] == "SUBSCRIPTION_WITHDRAWAL"
    assert fmt["proto"]["map"]["132"] == "SCTP"
    assert fmt["stime"]["kind"] == "time" and fmt["status"]["kind"] == "plain"


@pytest.mark.parametrize("metric", ["failure_rate", "timeouts_per_minute"])
def test_diameter_metrics_run_on_the_generated_database(generated_db, metric):
    sql = SemanticLayer(REAL).data["metrics"][metric]["sql"]
    con = sqlite3.connect(generated_db)
    result = con.execute(sql.replace("<start>", str(START_MS)).replace("<end>", str(END_MS))).fetchall()
    con.close()
    assert result
    if metric == "failure_rate":
        assert all(0 <= rate <= 1 for _command, rate in result) and any(rate > 0 for _command, rate in result)


def test_dns_metadata_is_a_telemetry_table_with_named_codes():
    layer = SemanticLayer(REAL)
    assert "dns_metadata" in layer.table_names()
    fmt = layer.column_formats(["rtype", "qtype", "rcode", "opcode", "proto", "etime"])
    assert fmt["rtype"]["kind"] == "enum" and fmt["rtype"]["map"]["28"] == "AAAA"
    assert fmt["qtype"]["map"]["1"] == "A" and fmt["rcode"]["map"]["3"] == "NXDOMAIN"
    assert fmt["opcode"]["map"]["4"] == "NOTIFY" and fmt["proto"]["map"]["17"] == "UDP"
    assert fmt["etime"]["kind"] == "time"
    cols = layer.data["tables"]["dns_metadata"]["columns"]
    assert "enum" not in cols["flags"] and "enum" not in cols["rsection"]
    assert "top_names" in layer.data["metrics"]
