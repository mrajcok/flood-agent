"""Gx and Gy sessions, and which Subscriber held a flow's client IP: seen through `generate()`, the
database and the shipped Semantic Layer."""

import shutil
import sqlite3
from itertools import pairwise
from pathlib import Path

import pytest
from conftest import attack_entries, collect, sql_rows

from flood_agent.dev_tools import shift_to_now
from flood_agent.evalset.golden import load_golden, run_reference
from flood_agent.events import DisplayEvent
from flood_agent.fake_llm import FakeClient, load_canned_prompts
from flood_agent.generator import END_MS, START_MS, generate
from flood_agent.semantic_layer import SemanticLayer

REAL = Path(__file__).parent.parent / "semantic_layer.yaml"
GX, GY = 16777238, 4
DAY_MS = 86_400_000


def query(db, sql, *args):
    """Rows readable by position or by column name."""
    return sql_rows(db, sql, *args, row_factory=sqlite3.Row)


def sessions(db, interface):
    """{session_id: its records in the order sent}"""
    out = {}
    for r in query(db, "SELECT * FROM diameter WHERE application_id = ? ORDER BY stime", interface):
        out.setdefault(r["session_id"], []).append(r)
    return out


def span(records):
    """From the INITIAL's request to the TERMINATION's answer."""
    return (min(r["stime"] for r in records if r["cc_request_type"] == 1),
            max(r["etime"] for r in records if r["cc_request_type"] == 3))


def spans_by_ue_ip(db):
    out = {}
    for records in sessions(db, GX).values():
        out.setdefault(records[0]["ue_ip_address"], []).append((*span(records), records[0]["imsi"]))
    return {ip: sorted(held) for ip, held in out.items()}


def client_ips(db):
    """The client end of the baseline flows: the generator draws clients from 10.1-3.0.0/16."""
    ips = set()
    for column in ("sip", "dip"):
        ips |= {r[0] for r in query(db, f"SELECT DISTINCT {column} FROM flows")}
    return {ip for ip in ips if ip.split(".")[:2] in (["10", "1"], ["10", "2"], ["10", "3"])}


def test_a_fifth_of_the_flows_client_ips_are_ue_ips_and_the_rest_come_from_their_own_pool(diameter_db):
    ue_ips = set(spans_by_ue_ip(diameter_db))
    clients = client_ips(diameter_db)
    shared = ue_ips & clients
    assert 0.19 < len(shared) / len(clients) < 0.21
    assert all(ip.startswith("10.64.") for ip in ue_ips - shared)


def test_no_server_attacker_or_reflector_ip_is_a_ue_ip(diameter_db):
    attacks = attack_entries(diameter_db)
    ue_ips = set(spans_by_ue_ip(diameter_db))
    amp = next(a for a in attacks if a["type"] == "amplification")
    reflectors = query(
        diameter_db, "SELECT sip FROM flows WHERE dip = ? AND sip NOT LIKE '10.%'", amp["targets"][0]["dip"]
    )
    others = {r[0] for r in reflectors}
    others |= {t["dip"] for a in attacks for t in a.get("targets", [])}
    others |= {"192.0.2.99", "10.0.0.53"}  # the scanner and the tunnel's resolver
    others |= {f"203.0.113.{i}" for i in range(1, 255)} | {f"198.51.100.{i}" for i in range(1, 100)}
    assert not ue_ips & others


def test_a_shared_client_ip_is_held_by_exactly_one_gx_session_at_every_flow(diameter_db):
    held = spans_by_ue_ip(diameter_db)
    shared = set(held) & client_ips(diameter_db)
    flows = query(diameter_db, "SELECT sip AS ip, etime FROM flows UNION ALL SELECT dip, etime FROM flows")
    checked = 0
    for flow in flows:
        if flow["ip"] in shared:
            assert sum(start <= flow["etime"] <= end for start, end, _ in held[flow["ip"]]) == 1, dict(flow)
            checked += 1
    assert checked > 1000


def test_a_shared_client_ip_passes_between_subscribers_a_few_times_with_no_gap(diameter_db):
    held = spans_by_ue_ip(diameter_db)
    for ip in set(held) & client_ips(diameter_db):
        chain = held[ip]
        assert all(after[0] == before[1] + 1 for before, after in pairwise(chain)), ip
        handovers = sum(before[2] != after[2] for before, after in pairwise(chain))
        assert 2 <= handovers <= 5, ip


def test_no_ue_ip_is_held_by_two_sessions_at_once(diameter_db):
    for ip, held in spans_by_ue_ip(diameter_db).items():
        assert all(before[1] < after[0] for before, after in pairwise(held)), ip


def test_sessions_run_from_initial_to_termination_within_a_day(diameter_db):
    for interface in (GX, GY):
        found = sessions(diameter_db, interface)
        assert found
        for session_id, records in found.items():
            sent = [r for r in records if r["status"] != "Retransmission"]
            kinds = [r["cc_request_type"] for r in sent]
            assert kinds == [1, *[2] * (len(kinds) - 2), 3], session_id
            assert [r["cc_request_number"] for r in sent] == list(range(len(sent)))
            start, end = span(records)
            assert 0 < end - start <= DAY_MS
            # a request goes out only once the one before it is answered
            assert all(before["etime"] <= after["stime"] for before, after in pairwise(records))


def test_every_record_of_a_session_names_the_subscriber_its_address_and_apn(diameter_db):
    for interface in (GX, GY):
        for records in sessions(diameter_db, interface).values():
            for column in ("imsi", "msisdn", "ue_ip_address", "apn"):
                values = {r[column] for r in records}
                assert len(values) == 1 and None not in values, column
            assert records[0]["imsi"].startswith("00101")  # inbound roamers use S6a only
            assert records[0]["apn"] in ("internet", "ims")
            assert all(r["command_code"] == 272 and r["session_id"].startswith(r["origin_host"] + ";")
                       for r in records)


def test_each_gx_session_has_a_gy_mirror_of_its_own(diameter_db):
    gx, gy = sessions(diameter_db, GX), sessions(diameter_db, GY)
    assert len(gx) == len(gy) and not gx.keys() & gy.keys()

    def identity(records):
        return tuple(records[0][c] for c in ("imsi", "msisdn", "ue_ip_address", "apn"))

    mirrors = {}
    for records in gy.values():
        mirrors.setdefault(identity(records), []).append(span(records))
    for records in gx.values():
        start, end = span(records)
        assert any(start < s < start + 10_000 and abs(e - end) < 10_000 for s, e in mirrors[identity(records)])


def test_columns_that_do_not_apply_to_gx_and_gy_are_null(diameter_db):
    for column in ("imei", "visited_plmn_id", "cancellation_type", "experimental_result_code"):
        s6a_only = f"SELECT 1 FROM diameter WHERE application_id <> 16777251 AND {column} IS NOT NULL"
        assert query(diameter_db, s6a_only) == []
    assert query(diameter_db, "SELECT 1 FROM diameter WHERE application_id = ? AND rat_type IS NOT NULL", GY) == []
    rat = query(diameter_db, "SELECT cc_request_type, COUNT(rat_type) = COUNT(*), COUNT(rat_type) FROM diameter "
                    "WHERE application_id = ? GROUP BY cc_request_type", GX)
    assert [(kind, bool(every), count > 0) for kind, every, count in rat] == [
        (1, True, True), (2, False, False), (3, False, False)]


def test_about_two_percent_of_gy_records_hit_the_credit_limit_and_gx_has_no_errors(diameter_db):
    results = dict(query(diameter_db, "SELECT result_code, COUNT(*) FROM diameter WHERE application_id = ? "
                             "AND status <> 'Timeout' GROUP BY result_code", GY))
    assert set(results) == {2001, 4012}
    assert 0.015 < results[4012] / sum(results.values()) < 0.025
    assert [r[0] for r in query(diameter_db, "SELECT DISTINCT result_code FROM diameter WHERE application_id = ? "
                                    "AND status <> 'Timeout'", GX)] == [2001]


def test_the_semantic_layer_names_the_credit_control_request_types():
    layer = SemanticLayer(REAL)
    assert layer.column_formats(["cc_request_type"])["cc_request_type"]["map"] == {
        "1": "INITIAL_REQUEST", "2": "UPDATE_REQUEST", "3": "TERMINATION_REQUEST", "4": "EVENT_REQUEST"}


def flows_subscriber_join(db, start, end, where, *args):
    """Rows of the shipped metric over [start, end), filtered by `where`."""
    sql = SemanticLayer(REAL).data["metrics"]["flows_subscriber_join"]["sql"]
    sql = sql.replace("<start>", str(start)).replace("<end>", str(end))
    return [dict(r) for r in query(db, f"SELECT * FROM ({sql}) WHERE {where}", *args)]


def a_handover(db):
    """A shared client IP, and the first time it passes from one Subscriber to another."""
    held = spans_by_ue_ip(db)
    for ip in sorted(set(held) & client_ips(db)):
        for before, after in pairwise(held[ip]):
            if before[2] != after[2]:
                return ip, before, after
    raise AssertionError("no handover")


def test_flows_subscriber_join_finds_the_right_subscriber_across_a_reassignment(diameter_db):
    ip, before, after = a_handover(diameter_db)
    boundary = after[0]
    start, end = boundary - 6 * 3_600_000, boundary + 6 * 3_600_000
    found = flows_subscriber_join(diameter_db, start, end, "sip = ? OR dip = ?", ip, ip)
    flows = query(diameter_db, "SELECT etime FROM flows WHERE (sip = ? OR dip = ?) AND etime >= ? AND etime < ?",
                  ip, ip, start, end)
    assert len(found) == len(flows)  # each flow once
    for row in found:
        holder = [imsi for s, e, imsi in spans_by_ue_ip(diameter_db)[ip] if s <= row["etime"] <= e]
        assert [row["imsi"]] == holder
    assert {before[2], after[2]} <= {row["imsi"] for row in found}


def test_flows_subscriber_join_finds_a_session_that_began_before_the_window(diameter_db):
    ip, _, (start, end, imsi) = a_handover(diameter_db)
    flow = query(diameter_db, "SELECT etime FROM flows WHERE (sip = ? OR dip = ?) AND etime BETWEEN ? AND ? "
                     "ORDER BY etime DESC", ip, ip, start + (end - start) // 2, end)[0]["etime"]
    found = flows_subscriber_join(diameter_db, flow, flow + 1, "sip = ? OR dip = ?", ip, ip)
    assert found and {row["imsi"] for row in found} == {imsi}


def test_a_flows_subscriber_join_matches_the_same_rows_after_the_dev_time_shift(diameter_db, tmp_path):
    shifted = Path(shutil.copy(diameter_db, tmp_path / "shifted.db"))
    delta = shift_to_now(shifted, SemanticLayer(REAL), END_MS + 400 * DAY_MS)
    assert delta > 0

    def joined(path, by):
        rows = flows_subscriber_join(path, START_MS + by, END_MS + by, "dip < '10.1.2'")
        return sorted((r["imsi"], r["sip"], r["dip"], r["bytes"], r["etime"] - by) for r in rows)

    before = joined(diameter_db, 0)
    assert before and joined(shifted, delta) == before


GOLDEN = {q.id: q for q in load_golden()}


def test_the_by_interface_golden_counts_each_interface(diameter_db, tmp_path):
    columns, found = run_reference(diameter_db, tmp_path / "scratch.db", GOLDEN["diameter-by-interface"].sql)
    counts = {row[columns.index("application_id")]: row[columns.index("records")] for row in found}
    assert set(counts) == {16777251, GX, GY} and all(counts.values())


@pytest.mark.parametrize("scale", [0.01, 0.05])
def test_the_subscriber_for_flow_golden_names_who_held_the_client_ip_of_those_flows(scale, tmp_path):
    question = GOLDEN["diameter-subscriber-for-flow"]
    ip = "10.3.131.107"  # as the question names it, with 2026-01-03 10:00 to 13:00 UTC
    start, end = 1767434400000, 1767445200000
    assert ip in question.question and "2026-01-03 10:00 and 13:00 UTC" in question.question
    # The generator picks which client IPs are shared from the seed alone, and the golden set is
    # written for the default seed.
    seeded = tmp_path / "seeded.db"
    generate(seeded, seed=1, scale=scale)

    def answer():
        return run_reference(seeded, tmp_path / "scratch.db", question.sql)[1]

    (holder,) = {imsi for s, e, imsi in spans_by_ue_ip(seeded)[ip] if s < end and e >= start}
    assert answer() == [(holder,)]
    with sqlite3.connect(seeded) as con:  # the answer comes through the flows, not the sessions alone
        con.execute("DELETE FROM flows")
    assert answer() == []


@pytest.mark.generated_data
@pytest.mark.parametrize("prompt, columns", [
    ("Chart Diameter transactions per hour by interface", ["bucket_ms", "application_id", "records"]),
    ("Which Subscribers sent the most traffic?", ["imsi", "total_bytes"]),
])
async def test_the_canned_prompt_shows_its_display(make_agent, prompt, columns):
    canned = next(c for c in load_canned_prompts() if c.prompt == prompt)
    agent = await make_agent(FakeClient(load_canned_prompts(), chunk_delay_s=0), api_key=False)

    events = await collect(agent, canned.prompt)

    (shown,) = [e for e in events if isinstance(e, DisplayEvent)]
    table = agent.fetch_rows(shown.result_table)
    assert table["columns"] == columns and table["rows"]
