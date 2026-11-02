"""Generator behaviour: reproducible, attacks recorded, ordered, indexed, window written."""

import hashlib
import ipaddress
import json
import re
import shutil
import sqlite3
from pathlib import Path

import pytest
import yaml

from flood_agent.generator import generate

REAL_SL = Path(__file__).parent.parent / "semantic_layer.yaml"
DAY_MS = 86_400_000
SCALE = 0.01


@pytest.fixture(scope="module")
def built(tmp_path_factory):
    d = tmp_path_factory.mktemp("gen")
    sl = d / "semantic_layer.yaml"
    shutil.copy(REAL_SL, sl)
    db = d / "telemetry.db"
    attacks = generate(db, sl, seed=7, scale=SCALE)
    return db, sl, attacks


def rows(db, sql, *args):
    con = sqlite3.connect(db)
    try:
        return con.execute(sql, args).fetchall()
    finally:
        con.close()


def fingerprint(db):
    return hashlib.sha256(repr(rows(db, "SELECT * FROM flows ORDER BY rowid")).encode()).hexdigest()


def test_same_seed_gives_identical_database_and_attacks(built, tmp_path):
    db, _, attacks = built
    sl = tmp_path / "sl.yaml"
    shutil.copy(REAL_SL, sl)
    again = generate(tmp_path / "t.db", sl, seed=7, scale=SCALE)
    assert fingerprint(tmp_path / "t.db") == fingerprint(db)
    assert again == attacks
    generate(tmp_path / "o.db", sl, seed=8, scale=SCALE)
    assert fingerprint(tmp_path / "o.db") != fingerprint(db)


def test_spans_seven_days_with_diurnal_pattern(built):
    db, _, _ = built
    lo, hi = rows(db, "SELECT MIN(etime), MAX(etime) FROM flows")[0]
    assert 6.9 * DAY_MS < hi - lo <= 7 * DAY_MS
    by_hour = dict(rows(db, "SELECT (etime/3600000)%24, COUNT(*) FROM flows WHERE etime < ? GROUP BY 1",
                        lo + DAY_MS))
    assert max(by_hour.values()) > 1.5 * min(by_hour.values())


def test_full_scale_row_count_target_is_about_two_million():
    from flood_agent.generator import expected_rows
    assert 1_900_000 < expected_rows(1.0) < 2_300_000


def test_rows_in_etime_order_and_indexes_exist(built):
    db, _, _ = built
    etimes = [r[0] for r in rows(db, "SELECT etime FROM flows ORDER BY rowid")]
    assert etimes == sorted(etimes)
    idx = {r[0]: r[1] for r in rows(db, "SELECT name, sql FROM sqlite_master WHERE type='index'")}
    cols = {re.search(r"\((\w+)\)", s).group(1) for s in idx.values() if s}
    assert {"etime", "sip", "dip"} <= cols


def test_ips_are_ipv4_dotted_decimal_and_circuits_opaque(built):
    db, _, _ = built
    for col in ("sip", "dip"):
        for (v,) in rows(db, f"SELECT DISTINCT {col} FROM flows"):
            assert str(ipaddress.IPv4Address(v)) == v
    for (c,) in rows(db, "SELECT DISTINCT circuit FROM flows"):
        assert re.fullmatch(r"[A-Z0-9]{12}", c)


def test_three_attacks_recorded_with_parameters(built):
    db, _, attacks = built
    assert {a["type"] for a in attacks} == {"syn_flood", "amplification", "port_scan"}
    lo, hi = rows(db, "SELECT MIN(etime), MAX(etime) FROM flows")[0]
    for a in attacks:
        assert lo <= a["start"] < a["end"] <= hi + 1
        assert a["targets"] and a["circuits"]
        assert all(re.fullmatch(r"[A-Z0-9]{12}", c) for c in a["circuits"])


def test_attack_traffic_is_visible_in_the_data(built):
    db, _, attacks = built
    a = {x["type"]: x for x in attacks}
    syn = a["syn_flood"]
    n_in, n_sips = rows(db, "SELECT COUNT(*), COUNT(DISTINCT sip) FROM flows WHERE dip=? AND dport=? "
                            "AND (tcp_flags & 18)=2 AND etime BETWEEN ? AND ?",
                        syn["targets"][0]["dip"], syn["targets"][0]["dports"][0], syn["start"], syn["end"])[0]
    assert n_in >= 0.9 * syn["flow_count"] and n_sips > 50

    amp = a["amplification"]
    n, = rows(db, "SELECT COUNT(*) FROM flows WHERE dip=? AND proto=17 AND sport IN (53,123) "
                  "AND etime BETWEEN ? AND ?", amp["targets"][0]["dip"], amp["start"], amp["end"])[0]
    assert n >= 0.9 * amp["flow_count"]

    scan = a["port_scan"]
    sip = scan["sources"][0]
    n_ports, = rows(db, "SELECT COUNT(DISTINCT dport) FROM flows WHERE sip=? AND etime BETWEEN ? AND ?",
                    sip, scan["start"], scan["end"])[0]
    assert n_ports > 100
    assert rows(db, "SELECT COUNT(*) FROM flows WHERE sip=? AND (etime<? OR etime>?)",
                sip, scan["start"], scan["end"])[0][0] == 0


def test_attacks_are_written_as_machine_readable_json_beside_the_db(built):
    db, _, attacks = built
    assert json.loads(Path(str(db) + ".attacks.json").read_text()) == attacks


def test_generator_writes_window_into_semantic_layer_preserving_the_rest(built):
    db, sl, _ = built
    lo = rows(db, "SELECT MIN(etime) FROM flows")[0][0]
    data = yaml.safe_load(sl.read_text())
    orig = yaml.safe_load(REAL_SL.read_text())
    assert data["window_start"] == 1767225600000 and data["window_end"] == 1767225600000 + 7 * DAY_MS
    assert lo >= data["window_start"]
    assert {k: v for k, v in data.items() if not k.startswith("window_")} == \
           {k: v for k, v in orig.items() if not k.startswith("window_")}
