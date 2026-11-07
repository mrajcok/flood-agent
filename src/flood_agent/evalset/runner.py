"""Eval runner: drives the Agent turn entrypoint per model and question, compares the
Result Tables the LLM produced against reference results, and reports per-model
correctness, retries, latency and cost. On demand only; never part of CI or default pytest.

    export OPENROUTER_API_KEY=...   # or put it in .env (see .env.example)
    uv run python -m flood_agent.evalset --db data/telemetry.db \\
        --models anthropic/claude-sonnet-5.5 z-ai/glm-5.3 --out eval-report

The runner is Agent-side harness code: it may read Result Tables (like the UI does). The
LLM only ever sees what the Agent normally sends (Semantic Layer prompt, the question,
Query Metadata). A question is correct if ANY successful Result Table the LLM created in
the turn matches the reference under the rules in `compare.py`, or ANY successful
`run_count_query` number matches a one-cell reference (a count makes no Result Table). `retries` counts failed
run_query attempts (the Agent's failed-attempt counter).
"""

import argparse
import asyncio
import json
import re
import sys
import time
from dataclasses import asdict, dataclass, replace
from pathlib import Path

from pydantic import ValidationError

from flood_agent import queries
from flood_agent.agent import Agent, Conversation, TraceEntry, TraceReady, TurnError
from flood_agent.config import Settings
from flood_agent.evalset.client import UsageMeter
from flood_agent.evalset.compare import DEFAULT_TOLERANCE, Comparison, compare
from flood_agent.evalset.golden import Question, load_golden, run_reference
from flood_agent.llm import OpenRouterClient

# Row cap for eval runs (the UI picker is the only other source of a cap); large so no golden answer is truncated.
EVAL_MAX_RESULT_ROWS = 1_000_000

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
    count_queries: int = 0  # successful run_count_query answers (they create no Result Table)


def _count_answers(trace: dict | None) -> list[int]:
    """The numbers of the successful run_count_query calls in the turn: the LLM was told these
    directly and no Result Table exists for them, so they are judged as one-cell answers."""
    counts = []
    for e in (trace or {}).get("entries", []):
        if e["kind"] == "tool_call" and e["name"] == "run_count_query":
            try:
                result = json.loads(e["query_metadata"])
            except ValueError:
                continue
            if result.get("success"):
                counts.append(result["count"])
    return counts


def _judge(q: Question, ref, results_db: Path, names: list[str], counts: list[int]) -> tuple[bool, str, int, str | None, int]:
    if not names and not counts:
        return False, "no Result Table or count answer was produced", 0, None, 0
    ref_cols, ref_rows = ref
    tol = q.tolerance if q.tolerance is not None else DEFAULT_TOLERANCE
    last: Comparison | None = None
    for name in names:
        cols, rows = queries.iter_all_rows(results_db, name)
        c = compare(ref_cols, ref_rows, cols, list(rows), q.ordered, tol)
        if c.ok:
            return True, "", c.extra_columns, name, len(names)
        last = c
    for n in counts:
        c = compare(ref_cols, ref_rows, ["count"], [(n,)], q.ordered, tol)
        if c.ok:
            return True, "", 0, "run_count_query", len(names)
        last = c
    assert last is not None  # a table or a count was compared
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
        replace(Settings.from_defaults().agent_config(), telemetry_db=telemetry_db, results_db=results_db,
                semantic_layer=semantic_layer, model=model, app_db=app_db),
        lambda _model: llm,
    )
    retries, error, trace = 0, None, None
    conversation = Conversation()
    start = time.perf_counter()
    try:
        async with agent:
            async for event in agent.run_turn(conversation, q.question, EVAL_MAX_RESULT_ROWS):
                if isinstance(event, TraceEntry) and event.kind == "query_error":
                    retries += 1
                elif isinstance(event, TraceReady):
                    trace = event.trace
                elif isinstance(event, TurnError):
                    error = event.detail
    except Exception as e:  # a failed model call is a result, not a crash of the whole run
        error = _describe(e)
    latency = time.perf_counter() - start
    usage = llm.usage
    counts = [] if error else _count_answers(trace)
    if error:
        ok, reason, extra, matched, n = False, "turn failed", 0, None, 0
    else:
        names = [t.name for t in agent.tables.tables_of(conversation.id)] if conversation.id else []
        ok, reason, extra, matched, n = _judge(q, ref, results_db, names, counts)
    return QuestionResult(
        q.id, model, ok, reason, retries, latency, usage.get("cost"), usage.get("prompt_tokens"),
        usage.get("completion_tokens"), extra, error, matched, n, len(counts),
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
        cost = None if any(c is None for c in costs) else sum(c for c in costs if c is not None)
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
        "# Flood Agent eval report", "",
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
    p.add_argument("--db", help="generated Telemetry Database (seed 1); default: the telemetry_db setting")
    p.add_argument("--semantic-layer", help="must carry the same window as --db; default: the semantic_layer setting")
    p.add_argument("--models", nargs="+", default=CANDIDATE_MODELS)
    p.add_argument("--questions", nargs="+", help="only these question ids")
    p.add_argument("--out", default="eval-report", help="writes <out>.md and <out>.json")
    p.add_argument("--workdir", default="data/eval-work", help="scratch results/app databases")
    a = p.parse_args(argv)
    try:
        settings = Settings()  # pyright: ignore[reportCallIssue]  # pydantic-settings fills fields from env/TOML
    except ValidationError as e:
        print(f"invalid settings:\n{e}", file=sys.stderr)
        return 2
    key = settings.openrouter_api_key
    if key is None:
        print("OPENROUTER_API_KEY is not set; the eval runner calls real OpenRouter models.", file=sys.stderr)
        return 2
    questions = load_golden()
    if a.questions:
        questions = [q for q in questions if q.id in a.questions]
        if not questions:
            print("no matching question ids", file=sys.stderr)
            return 2
    results = asyncio.run(
        run_eval(a.models, questions, Path(a.db or settings.telemetry_db), Path(a.semantic_layer or settings.semantic_layer),
                 lambda model, qid: OpenRouterClient(key.get_secret_value(), model), Path(a.workdir))
    )
    summary = summarize(results)
    Path(f"{a.out}.md").write_text(render_markdown(summary, results))
    Path(f"{a.out}.json").write_text(render_json(summary, results))
    print(render_markdown(summary, results).split("\n## Per question")[0])
    return 0
