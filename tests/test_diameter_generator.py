"""The generator's `diameter` table and its S6a signaling, seen through `generate()` and the database."""

import hashlib

from conftest import (
    DIAMETER_SCALE,
    DIAMETER_SEED,
    HOME_PLMN,
    HOME_REALM,
    attack_entries,
    diameter_records,
    sql_rows,
    table_rows,
)

from flood_agent.generator import END_MS, START_MS, generate

S6A = "application_id = 16777251"
# `flows` and `dns_metadata` for seed 7 at scale 0.01, as generated before Diameter existed.
FLOWS_BEFORE_DIAMETER = "2cdeded6d52f53d61d6443443b7ef660837b99955b65e2173c2e6bbb4fa47dbc"
DNS_BEFORE_DIAMETER = "8e67784a5e7506baa74738f4903c55a210b4d72e26d91158e6f5a90db811fa48"
COLUMNS = (
    "stime etime latency_ms status session_id hop_by_hop_id end_to_end_id application_id command_code "
    "origin_host origin_realm destination_host destination_realm sip dip sport dport proto result_code "
    "experimental_result_code imsi msisdn imei home_plmn_id visited_plmn_id ue_ip_address apn "
    "cc_request_type cc_request_number rat_type cancellation_type"
).split()


def test_diameter_has_its_columns_in_etime_order_and_indexed(diameter_db):
    assert [r[1] for r in sql_rows(diameter_db, "PRAGMA table_info(diameter)")] == COLUMNS
    etimes = [r[0] for r in sql_rows(diameter_db, "SELECT etime FROM diameter ORDER BY rowid")]
    assert etimes and etimes == sorted(etimes)
    indexed = {
        sql_rows(diameter_db, f"PRAGMA index_info({name})")[0][2]
        for (name,) in sql_rows(
            diameter_db, "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='diameter'"
        )
    }
    assert {"etime", "imsi", "ue_ip_address"} <= indexed


def test_same_seed_gives_an_identical_diameter_table(diameter_db, tmp_path):
    generate(tmp_path / "again.db", seed=DIAMETER_SEED, scale=DIAMETER_SCALE)
    assert table_rows(tmp_path / "again.db", "diameter") == table_rows(diameter_db, "diameter")
    generate(tmp_path / "other.db", seed=DIAMETER_SEED + 1, scale=DIAMETER_SCALE)
    assert table_rows(tmp_path / "other.db", "diameter") != table_rows(diameter_db, "diameter")


def test_flows_and_dns_metadata_are_byte_identical_to_a_build_without_diameter(tmp_path):
    generate(tmp_path / "t.db", seed=7, scale=0.01)
    for table, before in (("flows", FLOWS_BEFORE_DIAMETER), ("dns_metadata", DNS_BEFORE_DIAMETER)):
        assert hashlib.sha256(repr(table_rows(tmp_path / "t.db", table)).encode()).hexdigest() == before


def test_timeouts_have_no_answer_and_are_declared_after_five_seconds(diameter_db):
    timeouts = diameter_records(diameter_db, "status = 'Timeout'")
    baseline = outside_attacks(diameter_db)
    timed_out = diameter_records(diameter_db, f"status = 'Timeout' AND {baseline}")
    rate = len(timed_out) / len(diameter_records(diameter_db, baseline))
    assert 0.001 < rate < 0.008  # about 0.3% of requests
    for r in timeouts:
        assert r["etime"] == r["stime"] + 5000
        assert (r["latency_ms"], r["result_code"], r["experimental_result_code"]) == (None,) * 3
        if r["command_code"] == 316:  # a ULR's MSISDN comes with the answer
            assert r["msisdn"] is None


def test_each_timeout_is_followed_by_a_retransmission_sharing_its_end_to_end_id(diameter_db):
    timeouts = diameter_records(diameter_db, "status = 'Timeout'")
    again = {r["end_to_end_id"]: r for r in diameter_records(diameter_db, "status = 'Retransmission'")}
    assert timeouts and len(again) == len(timeouts)
    for first in timeouts:
        resent = again[first["end_to_end_id"]]
        assert resent["stime"] >= first["etime"] and resent["latency_ms"] > 0
        for column in ("session_id", "command_code", "imsi", "origin_host", "destination_realm", "sip", "dip"):
            assert resent[column] == first[column], column


def test_an_answered_record_has_exactly_one_result_code_and_its_answer_delay(diameter_db):
    answered = diameter_records(diameter_db, "status <> 'Timeout'")
    for r in answered:
        assert (r["result_code"] is None) != (r["experimental_result_code"] is None)
        assert r["latency_ms"] > 0 and abs(r["etime"] - r["stime"] - r["latency_ms"]) <= 1
    assert {r["status"] for r in answered} == {"Answered", "Retransmission"}


def test_about_two_percent_of_s6a_records_fail_with_a_3gpp_experimental_result(diameter_db):
    s6a = diameter_records(diameter_db, f"application_id = 16777251 AND {outside_attacks(diameter_db)}")
    failed = [r for r in s6a if r["experimental_result_code"] is not None]
    assert 0.01 < len(failed) / len(s6a) < 0.035
    assert {r["experimental_result_code"] for r in failed} == {5001, 5004, 5420}
    assert {r["result_code"] for r in s6a} == {2001, None}
    for r in failed:
        if r["experimental_result_code"] == 5004:  # roaming not allowed: only a roamer can be told that
            assert r["visited_plmn_id"] != r["home_plmn_id"]
        assert r["msisdn"] is None  # subscription data comes only with a successful answer


def test_a_destination_host_is_missing_only_when_a_timed_out_request_named_none(diameter_db):
    assert diameter_records(diameter_db, "status <> 'Timeout' AND destination_host IS NULL") == []
    timed_out = diameter_records(diameter_db, "status = 'Timeout' AND destination_host IS NULL")
    unnamed = {r["command_code"] for r in timed_out}
    assert unnamed and unnamed <= {316, 318}  # an MME's AIR or ULR names no Destination-Host


def outside_attacks(db):
    """A condition selecting the records outside every Diameter attack window: the baseline's."""
    return " AND ".join(
        f"stime NOT BETWEEN {a['start']} AND {a['end']}"
        for a in attack_entries(db) if a["type"].startswith("diameter_")
    )


def test_subscribers_are_home_inbound_roamers_and_outbound_roamers(diameter_db):
    where = dict(sql_rows(diameter_db, "SELECT imsi, home_plmn_id FROM diameter GROUP BY imsi"))
    assert len(where) == 250  # 5,000 at scale 1.0
    assert all(imsi.startswith(plmn) and len(imsi) == 15 and imsi.isdigit() for imsi, plmn in where.items())
    inbound = {imsi for imsi, plmn in where.items() if plmn != HOME_PLMN}
    assert 0.08 < len(inbound) / len(where) < 0.12
    assert 3 <= len({where[i] for i in inbound}) <= 4
    outbound = sql_rows(diameter_db, "SELECT COUNT(DISTINCT imsi) FROM diameter WHERE home_plmn_id = ? "
                        "AND visited_plmn_id <> ?", HOME_PLMN, HOME_PLMN)[0][0]
    assert 0.03 < outbound / len(where) < 0.07
    # inbound roamers visit the home network, and only it
    assert sql_rows(diameter_db, "SELECT DISTINCT visited_plmn_id FROM diameter WHERE home_plmn_id <> ? "
                    "AND visited_plmn_id IS NOT NULL", HOME_PLMN) == [(HOME_PLMN,)]


def realm_of(plmn):
    return f"epc.mnc{plmn[3:].zfill(3)}.mcc{plmn[:3]}.3gppnetwork.org"


def test_every_s6a_record_crosses_between_an_mme_and_the_subscribers_hss(diameter_db):
    for r in diameter_records(diameter_db, f"{S6A} AND {outside_attacks(diameter_db)}"):
        from_hss = r["command_code"] in (317, 319, 320)  # CLR, IDR, DSR
        hss_realm = r["origin_realm"] if from_hss else r["destination_realm"]
        mme_realm = r["destination_realm"] if from_hss else r["origin_realm"]
        assert hss_realm == realm_of(r["home_plmn_id"])
        if r["visited_plmn_id"]:
            assert mme_realm == realm_of(r["visited_plmn_id"])
        hss_port, hss_ip = (r["sport"], r["sip"]) if from_hss else (r["dport"], r["dip"])
        assert hss_port == 3868 and r["proto"] in (132, 6) and hss_ip
        assert r["origin_host"].endswith(r["origin_realm"])


def test_each_attach_is_an_air_then_a_ulr_that_describes_the_device(diameter_db):
    ulrs = diameter_records(diameter_db, "command_code = 316")
    assert ulrs
    for r in ulrs:
        assert r["rat_type"] in (1004, 1005, 1007) and len(r["imei"]) == 15 and r["imei"].isdigit()
        if r["status"] != "Timeout" and r["result_code"] == 2001:
            assert r["msisdn"].isdigit()
    first = {}
    for r in sorted(diameter_records(diameter_db, S6A), key=lambda r: r["stime"]):
        first.setdefault(r["imsi"], r)
    assert {r["command_code"] for r in first.values()} == {318}


def test_the_traffic_mix_has_every_s6a_command_mostly_attaches(diameter_db):
    counts = dict(sql_rows(
        diameter_db, f"SELECT command_code, COUNT(*) FROM diameter WHERE {S6A} GROUP BY command_code"
    ))
    assert set(counts) == {316, 317, 318, 319, 320, 321, 323}
    assert counts[318] + counts[316] > 0.75 * sum(counts.values())
    assert 11 * 250 < sum(counts.values()) < 16 * 250  # per Subscriber over the week


def test_columns_that_do_not_apply_are_null(diameter_db):
    for column in ("ue_ip_address", "apn", "cc_request_type", "cc_request_number"):
        assert diameter_records(diameter_db, f"{S6A} AND {column} IS NOT NULL") == []
    for column, commands in (("imei", "316"), ("rat_type", "316"), ("visited_plmn_id", "316, 318"),
                             ("msisdn", "316, 319"), ("cancellation_type", "317")):
        outside = f"{S6A} AND command_code NOT IN ({commands}) AND {column} IS NOT NULL"
        assert diameter_records(diameter_db, outside) == [], column


def test_normal_cancellations_follow_a_move_to_another_mme(diameter_db):
    clrs = diameter_records(diameter_db, f"command_code = 317 AND {outside_attacks(diameter_db)}")
    assert clrs
    for c in clrs:
        assert (c["cancellation_type"], c["origin_realm"], c["home_plmn_id"]) == (0, HOME_REALM, HOME_PLMN)
        update = diameter_records(
            diameter_db, f"command_code = 316 AND imsi = '{c['imsi']}' AND etime <= {c['stime']}"
        )[-1]
        assert update["result_code"] == 2001 and update["origin_host"] != c["destination_host"]
        assert c["stime"] - update["etime"] < 1000


def test_diameter_stays_inside_the_generated_week(diameter_db):
    (lo, hi), = sql_rows(diameter_db, "SELECT MIN(stime), MAX(etime) FROM diameter")
    assert lo >= START_MS and hi < END_MS
    by_hour = dict(sql_rows(diameter_db, "SELECT (etime / 3600000) % 24, COUNT(*) FROM diameter GROUP BY 1"))
    assert by_hour[15] > 1.5 * by_hour[3]  # the diurnal curve
