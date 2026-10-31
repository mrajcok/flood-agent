"""Eval runner: drives the Agent turn entrypoint per model and question, compares the
Result Tables the LLM produced against reference results, and reports per-model
correctness, retries, latency and cost. On demand only; never part of CI or default pytest.

    export OPENROUTER_API_KEY=...   # or put it in .env
    uv run python -m flood_defender.evalset --db data/telemetry.db \\
        --models anthropic/claude-sonnet-5.5 z-ai/glm-5.3 --out eval-report

The runner is Agent-side harness code: it may read Result Tables (like the UI does). The
LLM only ever sees what the Agent normally sends (Semantic Layer prompt, the question,
Query Metadata). A question is correct if ANY successful Result Table the LLM created in
the turn matches the reference under the rules in `compare.py`. `retries` counts failed
run_query attempts (the Agent's failed-attempt counter).
"""

import argparse
import asyncio
import json
import os
import re
import sqlite3
import sys
import time
from dataclasses import asdict, dataclass
from pathlib import Path

from flood_defender import queries
from flood_defender.agent import Agent, AgentConfig, Conversation, TraceEntry, TurnError
from flood_defender.config import load_env_file
from flood_defender.llm import OpenRouterClient
from flood_defender.evalset.client import UsageMeter
from flood_defender.evalset.compare import DEFAULT_TOLERANCE, compare
from flood_defender.evalset.golden import Question, load_golden, run_reference

CANDIDATE_MODELS = [
    "anthropic/claude-sonnet-5.5",
    "deepseek/deepseek-v4-pro-0813",
    "z-ai/glm-5.3",
    "qwen/qwen3.8-27b",
]


@dataclass
class QuestionResult:
    question_id: str
    model: str
    correct: bool
    reason: str
    retries: int
    latency_s: float
    cost: float | None
    prompt_tokens: int | None
    completion_tokens: int | None
    extra_columns: int
    error: str | None = None
    matched_table: str | None = None
    result_tables: int = 0


def _result_tables(results_db: Path) -> list[str]:
    if not results_db.exists():
        return []
    con = sqlite3.connect(f"file:{results_db}?mode=ro", uri=True)
    try:
        names = [r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY rowid")]
    finally:
        con.close()
    return [n for n in names if queries.RESULT_TABLE_RE.match(n)]


def _judge(q: Question, ref, results_db: Path) -> tuple[bool, str, int, str | None, int]:
    names = _result_tables(results_db)
    if not names:
        return False, "no Result Table was created", 0, None, 0
    ref_cols, ref_rows = ref
    tol = q.tolerance if q.tolerance is not None else DEFAULT_TOLERANCE
    last = None
    for name in names:
        cols, rows = queries.iter_all_rows(results_db, name)
        c = compare(ref_cols, ref_rows, cols, list(rows), q.ordered, tol)
        if c.ok:
            return True, "", c.extra_columns, name, len(names)
        last = c
    return False, last.reason, 0, None, len(names)


def _describe(e: BaseException) -> str:
    while isinstance(e, BaseExceptionGroup) and e.exceptions:
        e = e.exceptions[0]  # the MCP client wraps the real failure in a TaskGroup
    return f"{type(e).__name__}: {e}"


async def _run_question(model, q, ref, telemetry_db, semantic_layer, client_factory, workdir) -> QuestionResult:
    qdir = workdir / re.sub(r"[^A-Za-z0-9_.-]", "_", model) / q.id
    qdir.mkdir(parents=True, exist_ok=True)
    results_db, app_db = qdir / "results.db", qdir / "app.db"
    for p in (results_db, app_db):
        p.unlink(missing_ok=True)
    llm = UsageMeter(client_factory(model, q.id))
    agent = Agent(
        AgentConfig(telemetry_db=telemetry_db, results_db=results_db, semantic_layer=semantic_layer,
                    model=model, app_db=app_db),
        llm,
    )
    retries, error = 0, None
    start = time.perf_counter()
    try:
        async for event in agent.run_turn(Conversation(), q.question):
            if isinstance(event, TraceEntry) and event.kind == "query_error":
                retries += 1
            elif isinstance(event, TurnError):
                error = event.detail
    except Exception as e:  # a failed model call is a result, not a crash of the whole run
        error = _describe(e)
    latency = time.perf_counter() - start
    usage = llm.usage
    if error:
        ok, reason, extra, matched, n = False, "turn failed", 0, None, 0
    else:
        ok, reason, extra, matched, n = _judge(q, ref, results_db)
    return QuestionResult(
        q.id, model, ok, reason, retries, latency, usage.get("cost"), usage.get("prompt_tokens"),
        usage.get("completion_tokens"), extra, error, matched, n,
    )


async def run_eval(models, questions, telemetry_db, semantic_layer, client_factory, workdir) -> list[QuestionResult]:
    """client_factory(model, question_id) -> LLM client (optionally with a `usage` dict)."""
    workdir = Path(workdir)
    workdir.mkdir(parents=True, exist_ok=True)
    refs = {q.id: run_reference(telemetry_db, workdir / "reference.db", q.sql) for q in questions}
    out = []
    for model in models:
        for q in questions:
            out.append(
                await _run_question(model, q, refs[q.id], telemetry_db, semantic_layer, client_factory, workdir)
            )
    return out


def summarize(results: list[QuestionResult]) -> dict:
    summary: dict[str, dict] = {}
    for model in dict.fromkeys(r.model for r in results):
        rs = [r for r in results if r.model == model]
        costs = [r.cost for r in rs]
        cost = None if any(c is None for c in costs) else sum(costs)
        lat = sorted(r.latency_s for r in rs)
        summary[model] = {
            "total": len(rs),
            "correct": sum(r.correct for r in rs),
            "errors": sum(r.error is not None for r in rs),
            "retries": sum(r.retries for r in rs),
            "mean_latency_s": sum(lat) / len(lat),
            "max_latency_s": lat[-1],
            "cost": cost,
            "mean_cost": None if cost is None else cost / len(rs),
        }
    return summary


def _money(v) -> str:
    return "n/a" if v is None else f"${v:.4f}"


def render_markdown(summary: dict, results: list[QuestionResult]) -> str:
    lines = [
        "# Flood Defender eval report", "",
        "| Model | Correct | Accuracy | Retries | Errors | Mean latency | Max latency | Total cost | Cost/question |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for m, s in summary.items():
        lines.append(
            f"| {m} | {s['correct']}/{s['total']} | {s['correct'] / s['total']:.0%} | {s['retries']} | "
            f"{s['errors']} | {s['mean_latency_s']:.1f}s | {s['max_latency_s']:.1f}s | "
            f"{_money(s['cost'])} | {_money(s['mean_cost'])} |"
        )
    lines += ["", "## Per question", "", "| Question | Model | Result | Retries | Latency | Cost | Note |",
              "|---|---|---|---|---|---|---|"]
    for r in results:
        note = r.error or r.reason
        lines.append(
            f"| {r.question_id} | {r.model} | {'pass' if r.correct else 'FAIL'} | {r.retries} | "
            f"{r.latency_s:.1f}s | {_money(r.cost)} | {note} |"
        )
    return "\n".join(lines) + "\n"


def render_json(summary: dict, results: list[QuestionResult]) -> str:
    return json.dumps({"summary": summary, "results": [asdict(r) for r in results]}, indent=2) + "\n"


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="Run the golden set against OpenRouter models.")
    p.add_argument("--db", default="data/telemetry.db", help="generated Telemetry Database (seed 1)")
    p.add_argument("--semantic-layer", default="semantic_layer.yaml", help="must carry the same window as --db")
    p.add_argument("--models", nargs="+", default=CANDIDATE_MODELS)
    p.add_argument("--questions", nargs="+", help="only these question ids")
    p.add_argument("--out", default="eval-report", help="writes <out>.md and <out>.json")
    p.add_argument("--workdir", default="data/eval-work", help="scratch results/app databases")
    a = p.parse_args(argv)
    load_env_file(".env", os.environ)
    key = os.environ.get("OPENROUTER_API_KEY")
    if not key:
        print("OPENROUTER_API_KEY is not set; the eval runner calls real OpenRouter models.", file=sys.stderr)
        return 2
    questions = load_golden()
    if a.questions:
        questions = [q for q in questions if q.id in a.questions]
        if not questions:
            print("no matching question ids", file=sys.stderr)
            return 2
    results = asyncio.run(
        run_eval(a.models, questions, Path(a.db), Path(a.semantic_layer),
                 lambda model, qid: OpenRouterClient(key, model), Path(a.workdir))
    )
    summary = summarize(results)
    Path(f"{a.out}.md").write_text(render_markdown(summary, results))
    Path(f"{a.out}.json").write_text(render_json(summary, results))
    print(render_markdown(summary, results).split("\n## Per question")[0])
    return 0
