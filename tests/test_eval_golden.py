"""The golden set: every reference query runs, is non-empty, and attack answers agree
with the generator's attacks.json."""

import json
import shutil
from pathlib import Path

import pytest

from flood_agent.evalset.golden import load_golden, run_reference
from flood_agent.generator import generate

REAL_SL = Path(__file__).parent.parent / "semantic_layer.yaml"
QUESTIONS = load_golden()


@pytest.fixture(scope="module")
def built(tmp_path_factory):
    d = tmp_path_factory.mktemp("golden")
    sl = d / "semantic_layer.yaml"
    shutil.copy(REAL_SL, sl)
    db = d / "telemetry.db"
    generate(db, sl, seed=1, scale=0.05)  # default seed, reduced volume
    return db, json.loads(Path(str(db) + ".attacks.json").read_text()), d


def test_golden_set_is_about_thirty_unique_questions():
    ids = [q.id for q in QUESTIONS]
    assert 28 <= len(ids) <= 36
    assert len(set(ids)) == len(ids)
    assert sum(1 for q in QUESTIONS if q.crosscheck) >= 8  # many target the attacks


@pytest.mark.parametrize("q", QUESTIONS, ids=lambda q: q.id)
def test_reference_sql_runs_and_is_non_empty(q, built):
    db, _, d = built
    columns, rows = run_reference(db, d / "scratch.db", q.sql)
    assert columns and rows


@pytest.mark.parametrize("q", [q for q in QUESTIONS if q.crosscheck], ids=lambda q: q.id)
def test_attack_answers_agree_with_attacks_json(q, built):
    db, attacks, d = built
    cc = q.crosscheck
    attack = next(a for a in attacks if a["type"] == cc["attack"])
    columns, rows = run_reference(db, d / "scratch.db", q.sql)
    value = rows[0][columns.index(cc["column"])]
    if "field" in cc:
        low = attack[cc["field"]]
        assert low <= value <= low + cc.get("slack", 0)
    else:
        allowed = {
            "circuits": attack["circuits"],
            "targets_dip": [t["dip"] for t in attack["targets"]],
            "sources": attack["sources"],
        }[cc["in"]]
        assert value in allowed
