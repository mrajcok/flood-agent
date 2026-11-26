"""Load the checked-in golden set and execute reference SQL."""

import math
import sys
from dataclasses import dataclass
from pathlib import Path

import yaml

from flood_agent.queries import QueryRunner

GOLDEN_PATH = Path(__file__).parent / "golden.yaml"


@dataclass
class Question:
    id: str
    question: str
    sql: str
    ordered: bool = False
    tolerance: float | None = None
    crosscheck: dict | None = None  # verified against attacks.json by the test suite
    # Cells above the question's AI Cell, top to bottom: {kind: markdown, text, send_to_ai}, {kind: sql, sql}
    # or {kind: show, command, table}. SQL and SHOW cells are run before the question.
    above: list[dict] | None = None


def load_golden(path: Path = GOLDEN_PATH) -> list[Question]:
    return [Question(**item) for item in yaml.safe_load(Path(path).read_text())]


def run_reference(telemetry_db: Path, scratch_results_db: Path, sql: str) -> tuple[list[str], list[tuple]]:
    """Run reference SQL the way the LLM's SQL runs, but with no time limit or row cap and no Result Table readable."""
    runner = QueryRunner(telemetry_db, scratch_results_db, math.inf, set, lambda name: None)
    made = runner.run(sql, sys.maxsize)
    if not made["success"]:
        raise RuntimeError(f"Reference SQL failed: {made.get('raw_error', made['error'])}")
    try:
        columns, rows = runner.all_rows(made["result_table"])
        return columns, list(rows)
    finally:
        runner.drop(made["result_table"])
