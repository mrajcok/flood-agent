"""Trace: per-turn transparency record, persisted with the turn, never sent to the LLM."""

import json
import re

from conftest import ScriptedLLM, collect, last_tool_result, tool_call

from flood_agent.agent import Conversation, TraceReady
from flood_agent.llm import TextChunk

SQL = "SELECT dport, SUM(bytes) AS total_bytes FROM flows GROUP BY dport ORDER BY total_bytes DESC"
SPEC = {"mark": "bar", "encoding": {"x": {"field": "dport", "type": "nominal"}}}


def chart_script():
    return [
        [tool_call("run_query", sql=SQL, title="t")],
        lambda msgs: [
            tool_call("show_chart", vega_lite_spec={**SPEC, "data": {"name": last_tool_result(msgs)["result_table"]}})
        ],
        [TextChunk("Chart shown.")],
    ]


async def run(make_agent, llm, message="q", conversation=None):
    agent = await make_agent(llm)
    events = await collect(agent, message, conversation)
    traces = [e for e in events if isinstance(e, TraceReady)]
    assert len(traces) == 1
    return agent, traces[0].trace


def of_kind(trace, kind):
    return [e for e in trace["entries"] if e["kind"] == kind]


async def test_trace_records_cached_tokens_per_step_and_in_totals(make_agent):
    llm = ScriptedLLM([tool_call("run_query", sql=SQL, title="t")], [TextChunk("Done.")])
    llm.cached_tokens = 80
    _, trace = await run(make_agent, llm)
    assert [s["usage"]["cached_tokens"] for s in of_kind(trace, "llm_step")] == [80, 80]
    assert trace["totals"]["cached_tokens"] == 160


async def test_success_turn_trace_has_every_item(make_agent):
    llm = ScriptedLLM(
        [TextChunk("Let me query."), tool_call("run_query", sql=SQL, title="t")],
        [TextChunk("Done.")],
    )
    agent, trace = await run(make_agent, llm)

    assert trace["model"] == "z-ai/glm-5.3"
    assert trace["system_prompt"] == llm.calls[0][0]["content"]
    assert "# Semantic Layer" in trace["system_prompt"]

    steps = of_kind(trace, "llm_step")
    assert len(steps) == 2
    assert steps[0]["raw_response"]["content"] == "Let me query."
    call = steps[0]["raw_response"]["tool_calls"][0]
    assert call["name"] == "run_query" and json.loads(call["arguments"])["sql"] == SQL
    assert steps[0]["usage"] == {"prompt_tokens": 100, "completion_tokens": 20, "cost": 0.0012, "cached_tokens": 0}
    assert steps[0]["duration_ms"] >= 0
    assert trace["totals"]["prompt_tokens"] == 200 and trace["totals"]["completion_tokens"] == 40
    assert trace["totals"]["cost"] == 0.0024

    (tc,) = of_kind(trace, "tool_call")
    assert tc["name"] == "run_query" and tc["sql"] == SQL and tc["duration_ms"] >= 0
    assert tc["executed_sql"].startswith("CREATE TABLE results.") and SQL in tc["executed_sql"]

    (rt,) = of_kind(trace, "result_table")
    assert rt["row_count"] == 2 and re.fullmatch(r"rt_\d{8}_\d{6}", rt["name"])
    assert rt["name"] == json.loads(tc["query_metadata"])["result_table"]


async def test_query_metadata_is_byte_identical_to_what_llm_received(make_agent):
    llm = ScriptedLLM([tool_call("run_query", sql=SQL, title="t")], [TextChunk("ok")])
    _, trace = await run(make_agent, llm)
    sent = [m for m in llm.calls[1] if m["role"] == "tool"][0]["content"]
    (tc,) = of_kind(trace, "tool_call")
    assert tc["query_metadata"] == sent


async def test_failing_then_retrying_turn_records_both_attempts_and_errors(make_agent):
    bad = "SELECT sip FROM flows WHERE sip = 172.30.41.7"
    llm = ScriptedLLM(
        [tool_call("run_query", sql=bad, title="t")],
        [tool_call("run_query", sql=SQL, title="t")],
        [TextChunk("ok")],
    )
    _, trace = await run(make_agent, llm)
    calls = of_kind(trace, "tool_call")
    assert [c["sql"] for c in calls] == [bad, SQL]
    assert "executed_sql" in calls[0]
    (err,) = of_kind(trace, "query_error")
    assert err["sql"] == bad and "near" in err["raw_error"]
    assert err["sanitized_error"] == last_tool_result(llm.calls[1])["error"]
    assert err["raw_error"] != err["sanitized_error"]
    assert calls[0]["query_metadata"] == [m for m in llm.calls[1] if m["role"] == "tool"][0]["content"]
    assert len(of_kind(trace, "result_table")) == 1


async def test_chart_turn_records_spec_before_and_after_resolution(make_agent):
    llm = ScriptedLLM(*chart_script())
    _, trace = await run(make_agent, llm)
    (cs,) = of_kind(trace, "chart_spec")
    table = of_kind(trace, "result_table")[0]["name"]
    assert cs["source_spec"]["data"] == {"name": table}
    assert cs["spec"]["data"] == {"url": f"/api/results/{table}/chart-rows"}


async def test_trace_never_reaches_the_llm(make_agent):
    bad = "SELECT sip FROM flows WHERE sip = 172.30.41.7"
    llm = ScriptedLLM(
        [tool_call("run_query", sql=bad, title="t")],
        [tool_call("run_query", sql=SQL, title="t")],
        lambda msgs: [tool_call("show_table", result_table=last_tool_result(msgs)["result_table"])],
        [TextChunk("ok")],
    )
    conv = Conversation()
    agent, trace = await run(make_agent, llm, "first", conv)
    raw = of_kind(trace, "query_error")[0]["raw_error"]
    executed = of_kind(trace, "tool_call")[1]["executed_sql"]

    follow = ScriptedLLM([TextChunk("fine")])
    agent.client_for = lambda model: follow
    await collect(agent, "second", agent.open_conversation(conv.id))
    sent = llm.all_sent_text() + follow.all_sent_text()
    for secret in [raw, executed, "executed_sql", "raw_error", "prompt_tokens", "duration_ms", "query_metadata"]:
        assert secret not in sent, secret


def ask(client, message, conversation_id=""):
    posted = client.post("/chat", data={"message": message, "conversation_id": conversation_id, "max_rows": "1000"})
    cid = re.search(r'name="conversation_id" value="([0-9a-f]+)"', posted.text).group(1)
    stream_url = posted.text.split('sse-connect="')[1].split('"')[0]
    return cid, client.get(stream_url).text


def test_trace_is_collapsed_details_in_stream_and_after_restart(build_agent, web_client):
    client = web_client(build_agent(ScriptedLLM(*chart_script())))
    cid, body = ask(client, "chart please")
    assert "event: trace" in body
    assert "<details" in body and body.count("open><summary>Result Tables and turn activity") == 1

    restarted = web_client(build_agent(ScriptedLLM()))
    page = restarted.get(f"/c/{cid}").text
    assert "<details" in page and page.count("open><summary>Result Tables and turn activity") == 1
    assert page.count('class="trace"') == 1
    assert page.count('class="trace-part"') >= 3
    assert '<details class="trace-part" open><summary>Result Tables' in page
    assert "CREATE TABLE results." in page and "z-ai/glm-5.3" in page


def test_trace_content_is_html_escaped(build_agent, web_client):
    sql = "SELECT '<script>alert(1)</script>' AS x FROM flows"
    llm = ScriptedLLM([tool_call("run_query", sql=sql, title="t")], [TextChunk("<b>hi</b>")])
    client = web_client(build_agent(llm))
    cid, body = ask(client, "q")
    page = client.get(f"/c/{cid}").text
    for text in (body, page):
        assert "<script>alert(1)</script>" not in text
        assert "&lt;script&gt;alert(1)&lt;/script&gt;" in text or "\\u003cscript" in text


def test_each_turn_has_its_own_trace_on_reopen(build_agent, web_client):
    llm = ScriptedLLM([TextChunk("one")], [TextChunk("two")])
    client = web_client(build_agent(llm))
    cid, _ = ask(client, "a")
    ask(client, "b", cid)
    assert client.get(f"/c/{cid}").text.count('class="trace"') == 2


def test_split_system_prompt_into_three_parts():
    from flood_agent.trace import split_system_prompt

    prompt = "rules\n# Semantic Layer\ntables: x\n\n# Result Tables\nNone.\n"
    head, layer, rest = split_system_prompt(prompt)
    assert head == "rules\n"
    assert layer == "# Semantic Layer\ntables: x\n"
    assert rest.startswith("# Result Tables")
    assert split_system_prompt(None) == ("", "", "")
    assert split_system_prompt("plain") == ("plain", "", "")


def test_only_a_changed_result_tables_section_is_called_a_change_and_every_system_part_collapses(
    build_agent, web_client
):
    client = web_client(build_agent(ScriptedLLM(*chart_script())))
    cid, _ = ask(client, "chart please")
    page = client.get(f"/c/{cid}").text
    # run_query adds a Result Table, so the second step's prompt differs in that section only
    assert "System prompt changed" not in page
    assert "Result Tables changed" in page
    changed = page.split("Result Tables changed")[1].split("</details>")[0]
    assert "# Result Tables" in changed and "# Semantic Layer" not in changed


def test_an_unchanged_system_prompt_is_not_reported_as_changed(build_agent, web_client):
    client = web_client(
        build_agent(ScriptedLLM([tool_call("run_count_query", sql="SELECT COUNT(*) FROM flows")], [TextChunk("n")]))
    )
    cid, _ = ask(client, "how many")
    page = client.get(f"/c/{cid}").text
    assert "changed" not in page.split('class="trace"')[1]


async def test_each_llm_step_records_how_many_bytes_were_sent_to_the_model(make_agent):
    llm = ScriptedLLM([tool_call("run_query", sql=SQL, title="t")], [TextChunk("Done.")])
    _, trace = await run(make_agent, llm)

    first, second = of_kind(trace, "llm_step")
    sent_first = len(json.dumps(llm.calls[0]).encode())
    assert sent_first <= first["request_bytes"] < sent_first + 20_000  # messages plus the tool definitions
    assert second["request_bytes"] > first["request_bytes"]  # the context grows with the tool result


def test_trace_shows_kilobytes_sent_after_each_llm_call(build_agent, web_client):
    client = web_client(build_agent(ScriptedLLM([TextChunk("hi")])))
    _, body = ask(client, "hello")
    assert re.search(r"LLM step &middot; \d+ ms &middot; [\d.]+ KB sent", body)
