"""Load the checked-in golden set and execute reference SQL."""

from dataclasses import dataclass
from pathlib import Path

import yaml

from flood_agent import queries

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
    """Run reference SQL through the same restricted connection the LLM's SQL uses."""
    con = queries.open_connection(telemetry_db, scratch_results_db)
    try:
        cur = con.execute(queries.validate_select(sql))
        return [d[0] for d in cur.description], cur.fetchall()
    finally:
        con.close()
