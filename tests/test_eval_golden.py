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
    package = Path(generator.__file__).parent
    sources = [*package.glob("generator*.py"), *sorted(package.glob("diameter_generator/*.py"))]
    key = hashlib.sha256(
        b"".join(p.read_bytes() for p in sorted(sources)) + REAL_SL.read_bytes() + f"{SEED} {SCALE}".encode()
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


def test_golden_set_is_a_few_dozen_unique_questions():
    ids = [q.id for q in QUESTIONS]
    assert 28 <= len(ids) <= 50
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
    if "equals" in cc:  # {result column: attacks.json field}, exact match on the first row
        for column, field in cc["equals"].items():
            assert rows[0][columns.index(column)] == attack[field]
        return
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


DNS_IDS = {"dns-total-records", "dns-top-names", "dns-records-by-rtype", "dns-tunnel",
           "amp-dns-name", "dns-ipv6-cidr", "dns-suffix"}


def test_diameter_questions_count_something(built):
    db, _, d = built
    by_id = {q.id: q for q in QUESTIONS}
    for name in ("diameter-total", "diameter-inbound-roamers", "diameter-timeouts"):
        _, rows = run_reference(db, d / "scratch.db", by_id[name].sql)
        assert rows[0][0] > 0, name


def test_dns_questions_are_in_the_set_and_the_two_joins_are_crosschecked():
    by_id = {q.id: q for q in QUESTIONS}
    assert DNS_IDS <= by_id.keys()
    assert by_id["dns-top-names"].ordered
    assert by_id["dns-tunnel"].crosscheck["attack"] == "dns_tunnel"
    assert by_id["amp-dns-name"].crosscheck["attack"] == "amplification"
