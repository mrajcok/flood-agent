"""The Conversation id travels in MCP request _meta, never in the launch environment or a tool schema."""

import json
import os
import sys

import pytest
from conftest import ScriptedLLM, TextChunk, collect, last_tool_result, tool_call
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from flood_defender.agent import Conversation

META_KEY = "flood_defender/conversation_id"


def _server_children() -> list[int]:
    me, found = os.getpid(), []
    for pid in filter(str.isdigit, os.listdir("/proc")):
        try:
            with open(f"/proc/{pid}/stat") as f:
                if int(f.read().rsplit(")", 1)[1].split()[1]) != me:
                    continue
            with open(f"/proc/{pid}/cmdline", "rb") as f:
                if b"flood_defender.mcp_server" in f.read():
                    found.append(int(pid))
        except (OSError, IndexError, ValueError):
            continue
    return found


@pytest.mark.subprocess
async def test_launch_environment_has_no_conversation_id(make_agent):
    seen = {}

    def peek(messages):
        (pid,) = _server_children()
        with open(f"/proc/{pid}/environ", "rb") as f:
            seen["env"] = dict(kv.split(b"=", 1) for kv in f.read().split(b"\0") if b"=" in kv)
        return [TextChunk("ok")]

    await collect(await make_agent(ScriptedLLM(peek)), "hi")
    assert b"TELEMETRY_DB" in seen["env"]
    assert b"CONVERSATION_ID" not in seen["env"]


@pytest.mark.subprocess
async def test_no_tool_schema_mentions_a_conversation_id(make_agent):
    seen = {}

    class Spy(ScriptedLLM):
        async def stream_chat(self, messages, tools):
            seen["tools"] = json.dumps(tools).lower()
            async for c in super().stream_chat(messages, tools):
                yield c

    await collect(await make_agent(Spy([TextChunk("ok")])), "hi")
    assert "conversation" not in seen["tools"]
    assert "_meta" not in seen["tools"]


async def _raw_calls(agent, calls):
    """Call the MCP server directly: [(tool, args, meta)] -> parsed results."""
    cfg = agent.config
    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "flood_defender.mcp_server"],
        env={
            "TELEMETRY_DB": str(cfg.telemetry_db),
            "RESULTS_DB": str(cfg.results_db),
            "APP_DB": str(agent.store.path),
            "CHART_ROW_CAP": str(cfg.chart_row_cap),
            "QUERY_TIMEOUT_S": str(cfg.query_timeout_s),
            "MAX_RESULT_ROWS": str(cfg.max_result_rows),
        },
    )
    out = []
    async with stdio_client(params) as (r, w), ClientSession(r, w) as session:
        await session.initialize()
        for name, args, meta in calls:
            res = await session.call_tool(name, args, meta=meta)
            out.append(json.loads(res.content[0].text))
    return out


@pytest.mark.subprocess
async def test_tool_calls_see_only_tables_of_the_meta_conversation(make_agent):
    results = []
    llm = ScriptedLLM(
        [tool_call("run_query", sql="SELECT dport FROM flows", title="t")],
        lambda m: results.append(last_tool_result(m)) or [TextChunk("done")],
    )
    agent = await make_agent(llm)
    conv = Conversation()
    await collect(agent, "q", conv)
    table = results[0]["result_table"]
    q = {"sql": f"SELECT * FROM {table}", "title": "again"}
    own, missing, wrong, empty = await _raw_calls(
        agent,
        [
            ("run_query", q, {META_KEY: conv.id}),
            ("run_query", q, None),
            ("show_table", {"result_table": table}, {META_KEY: "someone-else"}),
            ("show_table", {"result_table": table}, {META_KEY: ""}),
        ],
    )
    assert own["success"]
    assert not missing["success"]
    assert wrong == {"success": False, "error": "Unknown Result Table."}
    assert empty == wrong
