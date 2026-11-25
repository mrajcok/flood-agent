"""The HSS signaling flood in the `diameter` table, seen through `generate()` and attacks.json."""

from datetime import UTC, datetime

import pytest
from conftest import DIAMETER_SCALE, DIAMETER_SEED, HOME_REALM, attack_entries, sql_rows

from flood_agent import diameter_generator
from flood_agent.diameter_generator import hss_flood
from flood_agent.generator import generate

HSS = f"hss01.{HOME_REALM}"
TOO_BUSY = 3004  # result_code DIAMETER_TOO_BUSY


@pytest.fixture(scope="module")
def built(diameter_db):
    return diameter_db, next(a for a in attack_entries(diameter_db) if a["type"] == "diameter_hss_flood")


def ms(*when):
    return int(datetime(*when, tzinfo=UTC).timestamp() * 1000)


def test_the_attack_entry_names_the_window_and_the_partner_host(built):
    _, flood = built
    assert (flood["start"], flood["end"]) == (ms(2026, 1, 5, 16, 0), ms(2026, 1, 5, 16, 20) - 1)
    assert flood["source_host"].endswith("." + flood["source_realm"])
    assert flood["source_realm"] != HOME_REALM


def test_the_flood_hosts_airs_match_the_attack_entry(built):
    db, flood = built
    sent = sql_rows(db, "SELECT command_code, origin_realm, destination_host, home_plmn_id, stime, etime "
                        "FROM diameter WHERE origin_host = ?", flood["source_host"])
    assert len(sent) == flood["record_count"] > 20 * 20  # far more than the baseline's few a minute
    for command, realm, destination, plmn, stime, etime in sent:
        assert (command, realm, destination, plmn) == (318, flood["source_realm"], HSS, "00101")
        assert flood["start"] <= stime and etime <= flood["end"]


def test_too_busy_answers_come_only_in_the_window_to_requests_naming_the_hss(built):
    db, flood = built
    busy = sql_rows(db, "SELECT origin_host, destination_host, stime, etime, msisdn FROM diameter "
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
    counts = dict(sql_rows(db, f"SELECT etime BETWEEN ? AND ?, COUNT(*) FROM diameter WHERE status = 'Timeout' "
                               f"AND origin_host {sent_by} ? GROUP BY 1",
                           flood["start"], flood["end"], flood["source_host"]))
    week_minutes = 7 * 24 * 60
    assert counts[1] / minutes > 10 * counts.get(0, 0) / (week_minutes - minutes)


def test_the_attack_only_adds_airs_in_its_window(built, tmp_path, monkeypatch):
    db, flood = built
    others = tuple(s for s in diameter_generator.SOURCES if s is not hss_flood.attack)
    monkeypatch.setattr(diameter_generator, "SOURCES", others)
    without = tmp_path / "without.db"
    generate(without, seed=DIAMETER_SEED, scale=DIAMETER_SCALE)
    baseline = set(sql_rows(without, "SELECT * FROM diameter"))
    everything = set(sql_rows(db, "SELECT * FROM diameter"))
    assert baseline < everything
    columns = [r[1] for r in sql_rows(db, "PRAGMA table_info(diameter)")]
    for added in (dict(zip(columns, r, strict=True)) for r in everything - baseline):
        assert (added["command_code"], added["destination_realm"]) == (318, HOME_REALM)
        assert flood["start"] <= added["stime"] and added["etime"] <= flood["end"]
