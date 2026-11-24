"""What the LLM is sent for each run_query / run_count_query outcome, as exact strings (byte-identical to
before the tool outcome codec), and where the internal parts (raw error, executed CTAS) go instead."""
import json
from types import SimpleNamespace

from conftest import ScriptedLLM, collect, tool_call
from mcp.types import TextContent

from flood_agent.events import TraceReady
from flood_agent.llm import TextChunk
from flood_agent.mcp_connection import McpConnection

TRUNCATED_NOTE = (
    "The Result Table was truncated to its first 2 rows; there are probably more. Do not present it as the "
    "complete answer. For a top-N question, use ORDER BY with a LIMIT; otherwise aggregate (GROUP BY) or add filters."
)
COUNT_GUIDANCE = (
    "A count query must have the form SELECT COUNT(...) FROM ... (optionally COUNT(...) AS name) "
    "and return one non-negative integer, e.g. SELECT COUNT(*) FROM flows WHERE ..."
)


async def call(make_agent, name, sql, max_rows=1_000_000):
    """Run one query tool call in an AI Cell; return (the exact text the LLM was sent, agent, trace)."""
    llm = ScriptedLLM([tool_call(name, title="T", sql=sql)], [TextChunk("ok")])
    agent = await make_agent(llm)
    events = await collect(agent, "go", max_result_rows=max_rows)
    sent = [m for m in llm.calls[1] if m["role"] == "tool"][-1]["content"]
    (trace,) = [e.trace for e in events if isinstance(e, TraceReady)]
    return sent, agent, trace, llm


def entries(trace, kind):
    return [e for e in trace["entries"] if e["kind"] == kind]


async def test_run_query_success_is_sent_as_todays_exact_json(make_agent):
    sent, agent, trace, _ = await call(make_agent, "run_query", "SELECT dport FROM flows LIMIT 2")
    name = json.loads(sent)["result_table"]
    assert sent == (f'{{"success": true, "result_table": "{name}", "columns": [{{"name": "dport", "type": "INT"}}], '
                    '"row_count": 2}')
    assert name in agent.tables
    assert entries(trace, "result_table")[0]["name"] == name
    assert "CREATE TABLE" in entries(trace, "tool_call")[0]["executed_sql"]  # in the Trace ...
    assert "CREATE TABLE" not in sent  # ... never the LLM


async def test_run_query_truncated_is_sent_as_todays_exact_json(make_agent):
    sent, _, _, _ = await call(make_agent, "run_query", "SELECT dport FROM flows", max_rows=2)
    name = json.loads(sent)["result_table"]
    assert sent == (f'{{"success": true, "result_table": "{name}", "columns": [{{"name": "dport", "type": "INT"}}], '
                    f'"row_count": 2, "truncated_to": 2, "note": {json.dumps(TRUNCATED_NOTE)}}}')


async def test_rejected_query_is_sent_as_todays_exact_json_and_registers_nothing(make_agent):
    sent, agent, trace, _ = await call(make_agent, "run_query", "DROP TABLE flows")
    assert sent == '{"success": false, "error": "Only a single read-only SELECT statement is allowed."}'
    assert agent.tables.names() == []
    assert entries(trace, "result_table") == []


async def test_database_error_goes_raw_to_the_trace_and_sanitised_to_the_llm(make_agent):
    sent, _, trace, llm = await call(make_agent, "run_query", "SELECT nope FROM flows")
    assert sent == '{"success": false, "error": "no such column: nope"}'
    (entry,) = entries(trace, "query_error")
    assert entry["raw_error"] and entry["sanitized_error"] == "no such column: nope"
    assert "raw_error" not in llm.all_sent_text() and "executed_sql" not in llm.all_sent_text()
    assert "CREATE TABLE" not in llm.all_sent_text()


async def test_count_success_is_sent_as_todays_exact_json(make_agent):
    sent, _, _, _ = await call(make_agent, "run_count_query", "SELECT COUNT(*) FROM flows")
    assert sent == '{"success": true, "count": 3}'


async def test_count_wrong_shape_is_sent_as_todays_exact_json(make_agent):
    sent, _, _, _ = await call(make_agent, "run_count_query", "SELECT dport FROM flows")
    assert sent == json.dumps({"success": False, "error": COUNT_GUIDANCE})


async def test_unreadable_query_content_reaches_the_llm_only_as_a_generic_failure(make_agent, monkeypatch):
    async def garbage(self, name, args, max_rows):
        return SimpleNamespace(content=[TextContent(type="text", text="oops 10.1.2.3")], isError=False)

    monkeypatch.setattr(McpConnection, "call_tool", garbage)
    sent, agent, trace, llm = await call(make_agent, "run_query", "SELECT dport FROM flows")
    assert sent == '{"success": false, "error": "The tool returned an unreadable result."}'
    assert "10.1.2.3" not in llm.all_sent_text()
    assert "10.1.2.3" in json.dumps(trace)  # kept for diagnosis
    assert agent.tables.names() == []


async def test_unreadable_query_content_counts_toward_the_retry_limit(make_agent, monkeypatch):
    from flood_agent import ai_cell_run

    async def garbage(self, name, args, max_rows):
        return SimpleNamespace(content=[TextContent(type="text", text="oops 10.1.2.3")], isError=False)

    monkeypatch.setattr(McpConnection, "call_tool", garbage)
    call_ = tool_call("run_query", title="T", sql="SELECT dport FROM flows")
    llm = ScriptedLLM([call_], [call_], [call_])
    events = await collect(await make_agent(llm), "go")
    assert ai_cell_run.GIVE_UP_MESSAGE in "".join(getattr(e, "text", "") for e in events)
    (trace,) = [e.trace for e in events if isinstance(e, TraceReady)]
    assert len(entries(trace, "query_error")) == 3
    assert all(e["raw_error"] == "oops 10.1.2.3" for e in entries(trace, "query_error"))
