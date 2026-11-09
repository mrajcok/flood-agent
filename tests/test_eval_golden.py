"""The golden set: every reference query runs, is non-empty, and attack answers agree
with the generator's attacks.json."""

import hashlib
import json
import os
import shutil
import tempfile
from pathlib import Path

import pytest

from flood_agent import generator
from flood_agent.evalset.golden import load_golden, run_reference
from flood_agent.generator import generate

REAL_SL = Path(__file__).parent.parent / "semantic_layer.yaml"
QUESTIONS = load_golden()


SEED, SCALE = 1, 0.05  # default seed, reduced volume
GENERATED = ("telemetry.db", "telemetry.db.attacks.json", "semantic_layer.yaml")


def generated_once(cache_root: Path) -> Path:
    """The generator's output (it is deterministic), cached across xdist workers and runs, keyed
    by the generator's source, the real Semantic Layer and the seed and scale. Generating takes
    over a second; each worker would otherwise pay it."""
    key = hashlib.sha256(
        Path(generator.__file__).read_bytes() + REAL_SL.read_bytes() + f"{SEED} {SCALE}".encode()
    ).hexdigest()[:16]
    done = cache_root / key
    if not done.is_dir():
        work = Path(tempfile.mkdtemp(dir=cache_root))
        shutil.copy(REAL_SL, work / "semantic_layer.yaml")
        generate(work / "telemetry.db", seed=SEED, scale=SCALE)
        try:
            os.rename(work, done)  # atomic; fails if another worker got there first
        except OSError:
            shutil.rmtree(work)
    return done


@pytest.fixture(scope="module")
def built(tmp_path_factory, request):
    src = generated_once(request.config.cache.mkdir("golden-db"))
    d = tmp_path_factory.mktemp("golden")
    for name in GENERATED:
        shutil.copy(src / name, d / name)
    db = d / "telemetry.db"
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
