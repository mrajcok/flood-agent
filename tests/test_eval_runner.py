"""The eval runner, driven through the real Agent turn entrypoint with the scripted fake LLM."""

import json

import pytest
from conftest import ScriptedLLM, all_telemetry_values, mcp_children, tool_call

from flood_agent.evalset import runner
from flood_agent.evalset.golden import Question
from flood_agent.llm import TextChunk

Q = Question(
    id="top-dport",
    question="Which destination port carried the most bytes? Return dport and total bytes.",
    sql="SELECT dport, SUM(bytes) AS total FROM flows GROUP BY dport ORDER BY total DESC LIMIT 1",
)
GOOD = Q.sql
DONE = [TextChunk("done")]


def query(sql, call_id="c1"):
    return [tool_call("run_query", call_id, sql=sql, title="t")]


async def run_one(scripts, telemetry_db, semantic_layer_path, tmp_path, question=Q):
    llms = []

    def factory(model, question_id):
        llm = ScriptedLLM(*scripts)
        llms.append(llm)
        return llm

    results = await runner.run_eval(
        ["fake/model"], [question], telemetry_db, semantic_layer_path, factory, tmp_path / "work"
    )
    return results[0], llms[0]


async def test_right_answer_is_correct(telemetry_db, semantic_layer_path, tmp_path):
    r, _ = await run_one([query(GOOD), DONE], telemetry_db, semantic_layer_path, tmp_path)
    assert r.correct and r.retries == 0 and r.error is None
    assert r.latency_s > 0 and r.model == "fake/model" and r.question_id == "top-dport"


@pytest.mark.subprocess
async def test_eval_run_leaves_no_mcp_server_processes_behind(telemetry_db, semantic_layer_path, tmp_path):
    await run_one([query(GOOD), DONE], telemetry_db, semantic_layer_path, tmp_path)
    assert mcp_children() == []


async def test_different_column_order_and_names_still_correct(telemetry_db, semantic_layer_path, tmp_path):
    sql = "SELECT SUM(bytes) AS b, dport AS port FROM flows GROUP BY dport ORDER BY b DESC LIMIT 1"
    r, _ = await run_one([query(sql), DONE], telemetry_db, semantic_layer_path, tmp_path)
    assert r.correct and r.extra_columns == 0


async def test_extra_columns_still_correct_and_counted(telemetry_db, semantic_layer_path, tmp_path):
    sql = "SELECT dport, SUM(bytes) AS total, COUNT(*) AS n FROM flows GROUP BY dport ORDER BY total DESC LIMIT 1"
    r, _ = await run_one([query(sql), DONE], telemetry_db, semantic_layer_path, tmp_path)
    assert r.correct and r.extra_columns == 1


async def test_wrong_answer_is_incorrect(telemetry_db, semantic_layer_path, tmp_path):
    sql = "SELECT dport, SUM(bytes) AS total FROM flows GROUP BY dport ORDER BY total ASC LIMIT 1"
    r, _ = await run_one([query(sql), DONE], telemetry_db, semantic_layer_path, tmp_path)
    assert not r.correct and r.reason


async def test_failed_queries_then_success_counts_retries(telemetry_db, semantic_layer_path, tmp_path):
    scripts = [query("SELECT nope FROM flows", "a"), query("SELEC 1", "b"), query(GOOD, "c"), DONE]
    r, _ = await run_one(scripts, telemetry_db, semantic_layer_path, tmp_path)
    assert r.correct and r.retries == 2


async def test_giving_up_is_incorrect(telemetry_db, semantic_layer_path, tmp_path):
    bad = lambda i: query("SELECT nope FROM flows", f"c{i}")  # noqa: E731
    r, _ = await run_one([bad(1), bad(2), bad(3)], telemetry_db, semantic_layer_path, tmp_path)
    assert not r.correct and r.retries == 3 and "no Result Table" in r.reason


async def test_answering_without_a_query_is_incorrect(telemetry_db, semantic_layer_path, tmp_path):
    r, _ = await run_one([[TextChunk("It is 8443.")]], telemetry_db, semantic_layer_path, tmp_path)
    assert not r.correct and "no Result Table" in r.reason


async def test_any_successful_result_table_may_match(telemetry_db, semantic_layer_path, tmp_path):
    scripts = [query("SELECT 1 AS x", "a"), query(GOOD, "b"), DONE]
    r, _ = await run_one(scripts, telemetry_db, semantic_layer_path, tmp_path)
    assert r.correct


async def test_ordered_question_requires_order(telemetry_db, semantic_layer_path, tmp_path):
    q = Question(id="o", question="ports by bytes, biggest first", ordered=True,
                 sql="SELECT dport FROM flows GROUP BY dport ORDER BY SUM(bytes) DESC")
    asc = "SELECT dport FROM flows GROUP BY dport ORDER BY SUM(bytes) ASC"
    r, _ = await run_one([query(asc), DONE], telemetry_db, semantic_layer_path, tmp_path, question=q)
    assert not r.correct
    q2 = Question(**{**q.__dict__, "ordered": False})
    r, _ = await run_one([query(asc), DONE], telemetry_db, semantic_layer_path, tmp_path, question=q2)
    assert r.correct


async def test_llm_sees_only_the_question_and_no_telemetry_values(telemetry_db, semantic_layer_path, tmp_path):
    r, llm = await run_one([query(GOOD), DONE], telemetry_db, semantic_layer_path, tmp_path)
    sent = llm.all_sent_text()
    assert Q.question in sent
    assert "Return dport" in sent
    for value in all_telemetry_values(telemetry_db):
        if len(value) >= 4:
            assert value not in sent
    assert llm.tools_seen_names == ["run_query", "run_count_query", "describe_table", "show_table", "show_chart"]


async def test_cost_and_tokens_come_from_client_usage(telemetry_db, semantic_layer_path, tmp_path):
    # the scripted fake emits UsageChunk(100, 20, 0.0012) after each of the two LLM steps
    r, _ = await run_one([query(GOOD), DONE], telemetry_db, semantic_layer_path, tmp_path)
    assert r.cost == pytest.approx(0.0024) and r.prompt_tokens == 200 and r.completion_tokens == 40


async def test_client_exception_is_recorded_not_raised(telemetry_db, semantic_layer_path, tmp_path):
    def boom(messages):
        raise RuntimeError("HTTP 500")

    r, _ = await run_one([boom], telemetry_db, semantic_layer_path, tmp_path)
    assert not r.correct and "HTTP 500" in r.error


def test_summary_and_reports():
    R = runner.QuestionResult
    results = [
        R("q1", "m/a", True, "", 0, 2.0, 0.01, 10, 5, 0),
        R("q2", "m/a", False, "values: x", 2, 4.0, 0.03, 10, 5, 0),
        R("q1", "m/b", True, "", 1, 1.0, None, None, None, 1),
    ]
    summary = runner.summarize(results)
    a = summary["m/a"]
    assert a["correct"] == 1 and a["total"] == 2 and a["retries"] == 2
    assert a["mean_latency_s"] == pytest.approx(3.0) and a["cost"] == pytest.approx(0.04)
    assert summary["m/b"]["cost"] is None
    md = runner.render_markdown(summary, results)
    assert "m/a" in md and "50%" in md and "q2" in md
    data = json.loads(runner.render_json(summary, results))
    assert data["summary"]["m/a"]["correct"] == 1 and len(data["results"]) == 3


def test_main_requires_api_key(monkeypatch, capsys):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    assert runner.main(["--db", "x.db"]) == 2
    assert "OPENROUTER_API_KEY" in capsys.readouterr().err


async def test_eval_limits_ignore_the_operators_environment(telemetry_db, semantic_layer_path, tmp_path, monkeypatch):
    # Models must be scored under the same limits on every machine: defaults.toml, not the shell or .env.
    monkeypatch.setenv("MAX_FAILED_ATTEMPTS", "1")  # would make the Agent give up after the first failure
    scripts = [query("SELECT nope FROM flows", "a"), query(GOOD, "b"), DONE]
    r, _ = await run_one(scripts, telemetry_db, semantic_layer_path, tmp_path)
    assert r.correct and r.retries == 1


def test_main_reports_invalid_settings_instead_of_a_traceback(monkeypatch, capsys):
    monkeypatch.setenv("PORT", "abc")  # irrelevant to the runner, but still an invalid setting
    assert runner.main(["--db", "x.db"]) == 2
    assert "port" in capsys.readouterr().err.lower()


COUNT_Q = Question(
    id="total-flow-count",
    question="How many Flow Records are there in total? Return a single number.",
    sql="SELECT COUNT(*) AS flows FROM flows",
)


def count(sql, call_id="c1"):
    return [tool_call("run_count_query", call_id, sql=sql)]


async def test_a_right_run_count_query_answer_is_correct_without_a_result_table(
    telemetry_db, semantic_layer_path, tmp_path
):
    r, _ = await run_one([count(COUNT_Q.sql), DONE], telemetry_db, semantic_layer_path, tmp_path, COUNT_Q)
    assert r.correct and r.matched_table == "run_count_query" and r.result_tables == 0 and r.count_queries == 1


async def test_a_wrong_run_count_query_answer_is_incorrect(telemetry_db, semantic_layer_path, tmp_path):
    sql = "SELECT COUNT(*) FROM flows WHERE dport = 443"
    r, _ = await run_one([count(sql), DONE], telemetry_db, semantic_layer_path, tmp_path, COUNT_Q)
    assert not r.correct and r.count_queries == 1 and "row count" not in r.reason and r.reason


async def test_a_rejected_count_query_is_not_an_answer(telemetry_db, semantic_layer_path, tmp_path):
    r, _ = await run_one([count("SELECT dport FROM flows"), DONE], telemetry_db, semantic_layer_path, tmp_path, COUNT_Q)
    assert not r.correct and r.count_queries == 0 and r.reason == "no Result Table or count answer was produced"


async def test_a_count_does_not_answer_a_question_that_wants_a_table(telemetry_db, semantic_layer_path, tmp_path):
    r, _ = await run_one([count("SELECT COUNT(*) FROM flows"), DONE], telemetry_db, semantic_layer_path, tmp_path)
    assert not r.correct and r.count_queries == 1
