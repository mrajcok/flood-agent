"""The HSS signaling flood in the `diameter` table, seen through `generate()` and attacks.json."""

import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest

from flood_agent import diameter_generator
from flood_agent.diameter_generator.hss_flood import hss_flood
from flood_agent.generator import generate

SCALE = 0.05
HOME_REALM = "epc.mnc001.mcc001.3gppnetwork.org"
HSS = f"hss01.{HOME_REALM}"
TOO_BUSY = 3004  # result_code DIAMETER_TOO_BUSY


@pytest.fixture(scope="module")
def built(tmp_path_factory):
    db = tmp_path_factory.mktemp("hss_flood") / "telemetry.db"
    generate(db, seed=7, scale=SCALE)
    attacks = json.loads(Path(f"{db}.attacks.json").read_text())
    return db, next(a for a in attacks if a["type"] == "diameter_hss_flood")


def rows(db, sql, *args):
    con = sqlite3.connect(db)
    try:
        return con.execute(sql, args).fetchall()
    finally:
        con.close()


def ms(*when):
    return int(datetime(*when, tzinfo=UTC).timestamp() * 1000)


def test_the_attack_entry_names_the_window_and_the_partner_host(built):
    _, flood = built
    assert (flood["start"], flood["end"]) == (ms(2026, 1, 5, 16, 0), ms(2026, 1, 5, 16, 20) - 1)
    assert flood["source_host"].endswith("." + flood["source_realm"])
    assert flood["source_realm"] != HOME_REALM


def test_the_flood_hosts_airs_match_the_attack_entry(built):
    db, flood = built
    sent = rows(db, "SELECT command_code, origin_realm, destination_host, home_plmn_id, stime, etime "
                    "FROM diameter WHERE origin_host = ?", flood["source_host"])
    assert len(sent) == flood["record_count"] > 20 * 20  # far more than the baseline's few a minute
    for command, realm, destination, plmn, stime, etime in sent:
        assert (command, realm, destination, plmn) == (318, flood["source_realm"], HSS, "00101")
        assert flood["start"] <= stime and etime <= flood["end"]


def test_too_busy_answers_come_only_in_the_window_to_requests_naming_the_hss(built):
    db, flood = built
    busy = rows(db, "SELECT origin_host, destination_host, stime, etime, msisdn FROM diameter "
                    "WHERE result_code = ?", TOO_BUSY)
    for _, destination, stime, etime, msisdn in busy:
        assert destination == HSS and msisdn is None  # a refusal carries no subscription data
        assert flood["start"] <= stime and etime <= flood["end"]
    from_flood = sum(r[0] == flood["source_host"] for r in busy)
    assert from_flood > flood["record_count"] / 2  # many of the flood's own AIRs
    assert len(busy) - from_flood >= 5  # and legitimate requests too


@pytest.mark.parametrize("sent_by", ["=", "<>"], ids=["flood", "others"])
def test_timeouts_per_minute_rise_during_the_window(built, sent_by):
    db, flood = built
    minutes = (flood["end"] - flood["start"] + 1) / 60_000
    counts = dict(rows(db, f"SELECT etime BETWEEN ? AND ?, COUNT(*) FROM diameter WHERE status = 'Timeout' "
                           f"AND origin_host {sent_by} ? GROUP BY 1",
                       flood["start"], flood["end"], flood["source_host"]))
    week_minutes = 7 * 24 * 60
    assert counts[1] / minutes > 10 * counts.get(0, 0) / (week_minutes - minutes)


def test_the_attack_only_adds_airs_in_its_window(built, tmp_path, monkeypatch):
    db, flood = built
    others = tuple(s for s in diameter_generator.SOURCES if s is not hss_flood)
    monkeypatch.setattr(diameter_generator, "SOURCES", others)
    without = tmp_path / "without.db"
    generate(without, seed=7, scale=SCALE)
    baseline = set(rows(without, "SELECT * FROM diameter"))
    everything = set(rows(db, "SELECT * FROM diameter"))
    assert baseline < everything
    columns = [r[1] for r in rows(db, "PRAGMA table_info(diameter)")]
    for added in (dict(zip(columns, r, strict=True)) for r in everything - baseline):
        assert (added["command_code"], added["destination_realm"]) == (318, HOME_REALM)
        assert flood["start"] <= added["stime"] and added["etime"] <= flood["end"]
    for table in ("flows", "dns_metadata"):
        every_row = f"SELECT * FROM {table} ORDER BY rowid"
        assert rows(without, every_row) == rows(db, every_row)
