"""The spoofed Cancel-Location DoS in the Diameter data, seen through `generate()` and the database."""

import json
import sqlite3
from collections import Counter
from pathlib import Path

import pytest

from flood_agent import diameter_generator
from flood_agent.diameter_generator import clr_spoof
from flood_agent.evalset.golden import load_golden
from flood_agent.generator import generate

SCALE = 0.05  # 250 Subscribers, so 15 victims
HOME = "00101"
HOME_REALM = "epc.mnc001.mcc001.3gppnetwork.org"
START = 1767477600000  # 2026-01-03T22:00:00Z: day 2, counting from 0 as the other attack windows do
END = START + 30 * 60_000


@pytest.fixture(scope="module")
def db(tmp_path_factory):
    path = tmp_path_factory.mktemp("clr-spoof") / "telemetry.db"
    generate(path, seed=7, scale=SCALE)
    return path


@pytest.fixture(scope="module")
def attack(db):
    attacks = json.loads(Path(f"{db}.attacks.json").read_text())
    return next(a for a in attacks if a["type"] == "diameter_clr_spoof")


def records(db, where, *args):
    con = sqlite3.connect(db)
    con.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in con.execute(f"SELECT * FROM diameter WHERE {where} ORDER BY stime", args)]
    finally:
        con.close()


def foreign_clrs(db):
    return records(db, "command_code = 317 AND origin_realm <> ?", HOME_REALM)


def test_the_only_foreign_clrs_are_the_attack_and_they_match_its_entry(db, attack):
    assert (attack["start"], attack["end"]) == (START, END - 1)
    clrs = foreign_clrs(db)
    assert len(clrs) == attack["record_count"] > 0
    assert len({c["imsi"] for c in clrs}) == attack["victim_count"] == 15  # 300 at scale 1.0
    assert {(c["origin_realm"], c["origin_host"]) for c in clrs} == {
        (attack["source_realm"], attack["source_host"])
    }
    assert attack["source_host"].endswith(attack["source_realm"])
    assert all(START <= c["stime"] < END for c in clrs)


def test_the_attacker_withdraws_home_subscribers_at_the_home_mme_serving_them(db):
    for c in foreign_clrs(db):
        assert (c["cancellation_type"], c["home_plmn_id"], c["destination_realm"]) == (2, HOME, HOME_REALM)
        if c["status"] != "Timeout":
            assert c["result_code"] == 2001
        serving = records(
            db, "command_code = 316 AND result_code = 2001 AND imsi = ? AND etime <= ?", c["imsi"], c["stime"]
        )
        assert serving[-1]["visited_plmn_id"] == HOME
        assert c["destination_host"] == serving[-1]["origin_host"]


def test_every_victim_attaches_again_after_its_cancellation(db):
    for c in foreign_clrs(db):
        after = records(db, "imsi = ? AND stime >= ? AND stime < ?", c["imsi"], c["etime"], c["etime"] + 120_000)
        air = next(r for r in after if r["command_code"] == 318)
        assert any(
            r["command_code"] == 316 and r["result_code"] == 2001 and r["stime"] >= air["etime"] for r in after
        ), c["imsi"]


def dump(db, table):
    con = sqlite3.connect(db)
    try:
        return con.execute(f"SELECT * FROM {table} ORDER BY rowid").fetchall()
    finally:
        con.close()


def test_the_attack_only_adds_diameter_records(db, tmp_path, monkeypatch):
    without = tuple(s for s in diameter_generator.SOURCES if s is not clr_spoof.attack)
    monkeypatch.setattr(diameter_generator, "SOURCES", without)
    generate(tmp_path / "without.db", seed=7, scale=SCALE)
    for table in ("flows", "dns_metadata"):
        assert dump(tmp_path / "without.db", table) == dump(db, table)
    added = Counter(dump(db, "diameter"))
    added.subtract(dump(tmp_path / "without.db", "diameter"))
    assert min(added.values()) == 0  # every other record is still there, unchanged
    assert sum(added.values()) >= 3 * len(foreign_clrs(db))  # the CLRs, and an AIR and a ULR for each


def test_the_golden_question_is_crosschecked_against_the_attack():
    # tests/test_eval_golden.py runs the reference and compares its answer with attacks.json
    question = next(q for q in load_golden() if q.id == "diameter-clr-spoof")
    assert question.crosscheck["attack"] == "diameter_clr_spoof"
    assert set(question.crosscheck["equals"].values()) == {"source_realm", "victim_count"}
