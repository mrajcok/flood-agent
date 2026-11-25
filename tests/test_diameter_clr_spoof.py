"""The spoofed Cancel-Location DoS in the Diameter data, seen through `generate()` and the database."""

from collections import Counter

import pytest
from conftest import (
    DIAMETER_SCALE,
    DIAMETER_SEED,
    HOME_PLMN,
    HOME_REALM,
    attack_entries,
    diameter_records,
    table_rows,
)

from flood_agent import diameter_generator
from flood_agent.diameter_generator import clr_spoof
from flood_agent.generator import generate

START = 1767477600000  # 2026-01-03T22:00:00Z: day 2, counting from 0 as the other attack windows do
END = START + 30 * 60_000


@pytest.fixture(scope="module")
def attack(diameter_db):
    return next(a for a in attack_entries(diameter_db) if a["type"] == "diameter_clr_spoof")


def foreign_clrs(db):
    return diameter_records(db, "command_code = 317 AND origin_realm <> ?", HOME_REALM, order="stime")


def test_the_only_foreign_clrs_are_the_attack_and_they_match_its_entry(diameter_db, attack):
    assert (attack["start"], attack["end"]) == (START, END - 1)
    clrs = foreign_clrs(diameter_db)
    assert len(clrs) == attack["record_count"] > 0
    assert len({c["imsi"] for c in clrs}) == attack["victim_count"] == 15  # 300 at scale 1.0
    assert {(c["origin_realm"], c["origin_host"]) for c in clrs} == {
        (attack["source_realm"], attack["source_host"])
    }
    assert attack["source_host"].endswith(attack["source_realm"])
    assert all(START <= c["stime"] < END for c in clrs)


def test_the_attacker_withdraws_home_subscribers_at_the_home_mme_serving_them(diameter_db):
    for c in foreign_clrs(diameter_db):
        assert (c["cancellation_type"], c["home_plmn_id"], c["destination_realm"]) == (2, HOME_PLMN, HOME_REALM)
        if c["status"] != "Timeout":
            assert c["result_code"] == 2001
        serving = diameter_records(
            diameter_db, "command_code = 316 AND result_code = 2001 AND imsi = ? AND etime <= ?",
            c["imsi"], c["stime"],
        )
        assert serving[-1]["visited_plmn_id"] == HOME_PLMN
        assert c["destination_host"] == serving[-1]["origin_host"]


def test_every_victim_attaches_again_after_its_cancellation(diameter_db):
    for c in foreign_clrs(diameter_db):
        after = diameter_records(
            diameter_db, "imsi = ? AND stime >= ? AND stime < ?", c["imsi"], c["etime"], c["etime"] + 120_000,
            order="stime",
        )
        air = next(r for r in after if r["command_code"] == 318)
        assert any(
            r["command_code"] == 316 and r["result_code"] == 2001 and r["stime"] >= air["etime"] for r in after
        ), c["imsi"]


def test_the_attack_only_adds_diameter_records(diameter_db, tmp_path, monkeypatch):
    without = tuple(s for s in diameter_generator.SOURCES if s is not clr_spoof.attack)
    monkeypatch.setattr(diameter_generator, "SOURCES", without)
    generate(tmp_path / "without.db", seed=DIAMETER_SEED, scale=DIAMETER_SCALE)
    added = Counter(table_rows(diameter_db, "diameter"))
    added.subtract(table_rows(tmp_path / "without.db", "diameter"))
    assert min(added.values()) == 0  # every other record is still there, unchanged
    assert sum(added.values()) >= 3 * len(foreign_clrs(diameter_db))  # the CLRs, and an AIR and a ULR for each
