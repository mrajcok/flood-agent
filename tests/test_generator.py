"""Generator behaviour: reproducible, attacks recorded, ordered, indexed, no window written."""

import hashlib
import ipaddress
import json
import re
import sqlite3
from pathlib import Path

import pytest

from flood_agent.generator import generate

DAY_MS = 86_400_000
SCALE = 0.01


@pytest.fixture(scope="module")
def built(tmp_path_factory):
    d = tmp_path_factory.mktemp("gen")
    db = d / "telemetry.db"
    attacks = generate(db, seed=7, scale=SCALE)
    return db, attacks


def rows(db, sql, *args):
    con = sqlite3.connect(db)
    try:
        return con.execute(sql, args).fetchall()
    finally:
        con.close()


def fingerprint(db):
    return hashlib.sha256(repr(rows(db, "SELECT * FROM flows ORDER BY rowid")).encode()).hexdigest()


def test_same_seed_gives_identical_database_and_attacks(built, tmp_path):
    db, attacks = built
    again = generate(tmp_path / "t.db", seed=7, scale=SCALE)
    assert fingerprint(tmp_path / "t.db") == fingerprint(db)
    assert again == attacks
    generate(tmp_path / "o.db", seed=8, scale=SCALE)
    assert fingerprint(tmp_path / "o.db") != fingerprint(db)


def test_spans_seven_days_with_diurnal_pattern(built):
    db, _ = built
    lo, hi = rows(db, "SELECT MIN(etime), MAX(etime) FROM flows")[0]
    assert 6.9 * DAY_MS < hi - lo <= 7 * DAY_MS
    by_hour = dict(rows(db, "SELECT (etime/3600000)%24, COUNT(*) FROM flows WHERE etime < ? GROUP BY 1",
                        lo + DAY_MS))
    assert max(by_hour.values()) > 1.5 * min(by_hour.values())


def test_full_scale_row_count_target_is_about_two_million():
    from flood_agent.generator import expected_rows
    assert 1_900_000 < expected_rows(1.0) < 2_300_000


def test_rows_in_etime_order_and_indexes_exist(built):
    db, _ = built
    etimes = [r[0] for r in rows(db, "SELECT etime FROM flows ORDER BY rowid")]
    assert etimes == sorted(etimes)
    idx = {r[0]: r[1] for r in rows(db, "SELECT name, sql FROM sqlite_master WHERE type='index'")}
    cols = {re.search(r"\((\w+)\)", s).group(1) for s in idx.values() if s}
    assert {"etime", "sip", "dip"} <= cols


def test_ips_are_ipv4_dotted_decimal_and_circuits_opaque(built):
    db, _ = built
    for col in ("sip", "dip"):
        for (v,) in rows(db, f"SELECT DISTINCT {col} FROM flows"):
            assert str(ipaddress.IPv4Address(v)) == v
    for (c,) in rows(db, "SELECT DISTINCT circuit FROM flows"):
        assert re.fullmatch(r"[A-Z0-9]{12}", c)


def test_three_attacks_recorded_with_parameters(built):
    db, attacks = built
    assert {a["type"] for a in attacks} == {"syn_flood", "amplification", "port_scan"}
    lo, hi = rows(db, "SELECT MIN(etime), MAX(etime) FROM flows")[0]
    for a in attacks:
        assert lo <= a["start"] < a["end"] <= hi + 1
        assert a["targets"] and a["circuits"]
        assert all(re.fullmatch(r"[A-Z0-9]{12}", c) for c in a["circuits"])


def test_attack_traffic_is_visible_in_the_data(built):
    db, attacks = built
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
    db, attacks = built
    assert json.loads(Path(str(db) + ".attacks.json").read_text()) == attacks


JOIN = ("FROM dns_metadata d JOIN flows f ON d.sip=f.sip AND d.dip=f.dip AND d.sport=f.sport "
        "AND d.dport=f.dport AND d.proto=f.proto AND d.etime BETWEEN f.stime AND f.etime")
FLOWS_FINGERPRINT_SEED7_SCALE001 = "a654672131d374929b2e4645ebe3ba3e6e79837a36a9e988c6cb7034f61e4ea4"


def dns_columns():
    import yaml
    layer = yaml.safe_load((Path(__file__).parent.parent / "semantic_layer.yaml").read_text())
    return layer["tables"]["dns_metadata"]["columns"]


def test_flows_are_unchanged_by_the_dns_pass(built):
    db, _ = built
    assert fingerprint(db) == FLOWS_FINGERPRINT_SEED7_SCALE001


def test_dns_metadata_is_reproducible_ordered_and_indexed(built, tmp_path):
    db, _ = built
    again = tmp_path / "t.db"
    generate(again, seed=7, scale=SCALE)
    dump = "SELECT * FROM dns_metadata ORDER BY rowid"
    assert rows(db, dump) == rows(again, dump)
    etimes = [r[0] for r in rows(db, "SELECT etime FROM dns_metadata ORDER BY rowid")]
    assert etimes and etimes == sorted(etimes)
    idx = {re.search(r"\((\w+)\)", s).group(1)
           for (s,) in rows(db, "SELECT sql FROM sqlite_master WHERE type='index' AND tbl_name='dns_metadata'")}
    assert idx == {"etime", "sip", "dip"}


def test_dns_columns_and_values_follow_the_semantic_layer(built):
    db, _ = built
    layer = dns_columns()
    assert [r[1] for r in rows(db, "PRAGMA table_info(dns_metadata)")] == list(layer)
    assert {r[0] for r in rows(db, "SELECT DISTINCT qr FROM dns_metadata")} == {"R"}
    assert {r[0] for r in rows(db, "SELECT DISTINCT rcode FROM dns_metadata")} == {0}
    assert {r[0] for r in rows(db, "SELECT DISTINCT qtype FROM dns_metadata")} <= set(layer["qtype"]["enum"])
    assert {r[0] for r in rows(db, "SELECT DISTINCT rtype FROM dns_metadata")} <= set(layer["rtype"]["enum"])
    (ttl_lo, ttl_hi), = rows(db, "SELECT MIN(rttl), MAX(rttl) FROM dns_metadata")
    assert 30 <= ttl_lo and ttl_hi <= 86400
    suffixes = (".example.com", ".example.net", ".example.org", ".test", ".arpa")
    names = [r[0].lower() for r in rows(db, "SELECT DISTINCT question FROM dns_metadata")]
    assert len(names) > 20 and all(n.endswith(suffixes) for n in names)
    qtypes = dict(rows(db, "SELECT qtype, COUNT(*) FROM dns_metadata GROUP BY qtype"))
    assert qtypes[1] > qtypes[28] > 0


def test_about_half_of_dns_rows_join_to_a_flow_and_ipv6_rows_do_not(built):
    db, _ = built
    total = rows(db, "SELECT COUNT(*) FROM dns_metadata WHERE question <> 'amp.example.net'")[0][0]
    joined = rows(db, f"SELECT COUNT(DISTINCT d.rowid) {JOIN} WHERE d.question <> 'amp.example.net'")[0][0]
    assert 0.35 < joined / total < 0.65
    assert rows(db, "SELECT COUNT(*) FROM dns_metadata WHERE sip LIKE '%:%' OR dip LIKE '%:%'")[0][0] > 0
    assert rows(db, f"SELECT COUNT(*) {JOIN} WHERE d.sip LIKE '%:%' OR d.dip LIKE '%:%'")[0][0] == 0
    net = ipaddress.ip_network("2001:db8::/32")
    for (ip,) in rows(db, "SELECT DISTINCT sip FROM dns_metadata WHERE sip LIKE '%:%'"):
        assert ipaddress.IPv6Address(ip) in net


def amp_dns_rows(db, amp):
    return rows(db, "SELECT sip, dip, sport, dport, proto, tid, etime, question, qtype FROM dns_metadata "
                    "WHERE question LIKE ? AND etime BETWEEN ? AND ?", amp["dns_question"], amp["start"], amp["end"])


def test_every_amplification_dns_response_joins_to_an_attack_flow(built):
    db, attacks = built
    amp = next(a for a in attacks if a["type"] == "amplification")
    assert amp["dns_question"] == "amp.example.net" and amp["dns_qtype"] == 255
    victim = amp["targets"][0]["dip"]
    n_flows, = rows(db, "SELECT COUNT(*) FROM flows WHERE sport=53 AND dip=? AND etime BETWEEN ? AND ?",
                    victim, amp["start"], amp["end"])[0]
    responses, = rows(db, "SELECT COUNT(*) FROM (SELECT 1 FROM dns_metadata WHERE question=? "
                          "GROUP BY sip, dip, dport, tid, etime)", amp["dns_question"])[0]
    assert n_flows > 0 and responses == n_flows
    unjoined, = rows(db, "SELECT COUNT(*) FROM dns_metadata d WHERE d.question=? AND NOT EXISTS "
                         "(SELECT 1 FROM flows f WHERE d.sip=f.sip AND d.dip=f.dip AND d.sport=f.sport "
                         "AND d.dport=f.dport AND d.proto=f.proto AND d.etime BETWEEN f.stime AND f.etime "
                         "AND f.dip=?)", amp["dns_question"], victim)[0]
    assert unjoined == 0


def test_amplification_responses_have_10_to_30_records_of_mixed_types(built):
    db, attacks = built
    amp = next(a for a in attacks if a["type"] == "amplification")
    counts = [c for (c,) in rows(db, "SELECT COUNT(*) FROM dns_metadata WHERE question=? "
                                     "GROUP BY sip, dip, dport, tid, etime", amp["dns_question"])]
    assert counts and all(10 <= c <= 30 for c in counts)
    assert amp["dns_response_count"] == len(counts)
    assert {r[0] for r in rows(db, "SELECT DISTINCT qtype FROM dns_metadata WHERE question=?",
                               amp["dns_question"])} == {255}
    assert {r[0] for r in rows(db, "SELECT DISTINCT rtype FROM dns_metadata WHERE question=?",
                               amp["dns_question"])} <= {16, 1, 28, 2, 46}


def test_attacks_json_dns_fields_are_only_on_amplification(built):
    db, attacks = built
    on_disk = json.loads(Path(str(db) + ".attacks.json").read_text())
    assert on_disk == attacks
    for a in attacks:
        has = "dns_question" in a
        assert has == (a["type"] == "amplification")
        assert has == ("dns_qtype" in a) == ("dns_response_count" in a)


def flows_dns_join_sql(start, end):
    import yaml
    layer = yaml.safe_load((Path(__file__).parent.parent / "semantic_layer.yaml").read_text())
    return layer["metrics"]["flows_dns_join"]["sql"].replace("<start>", str(start)).replace("<end>", str(end))


def test_flows_dns_join_metric_finds_the_abused_name_over_the_attack_window(built):
    db, attacks = built
    amp = next(a for a in attacks if a["type"] == "amplification")
    result = rows(db, flows_dns_join_sql(amp["start"], amp["end"] + 1))
    assert any("amp.example.net" in r and 255 in r for r in result)
