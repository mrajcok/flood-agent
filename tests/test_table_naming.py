"""Result Table names: <title slug>_<YYYYMMDD>_<HHMMSS>, valid unquoted in SQLite and Ocient."""

import re
import sqlite3

import pytest
from conftest import query_limits

from flood_agent import queries

SQL = "SELECT dport, SUM(bytes) AS total_bytes FROM flows GROUP BY dport"
NAME = re.compile(r"^[a-z][a-z0-9_]*_\d{8}_\d{6}(_\d+)?$")


def run(telemetry_db, tmp_path, title):
    return queries.run_query(telemetry_db, tmp_path / "results.db", SQL, title=title, **query_limits())


@pytest.mark.parametrize("title, slug", [
    ("Top 10 dest IPs", "top_10_dest_ips"),
    ("  Bytes/port -- (UDP)  ", "bytes_port_udp"),
    ("évil_name; DROP TABLE flows", "vil_name_drop_table_flows"),
    ("10 biggest", "t_10_biggest"),
    ("!!!", "result"),
    ("", "result"),
])
def test_name_is_slug_plus_timestamp(telemetry_db, tmp_path, title, slug):
    meta = run(telemetry_db, tmp_path, title)
    name = meta["result_table"]
    assert NAME.match(name) and queries.RESULT_TABLE_RE.match(name), name
    assert name.rsplit("_", 2)[0] == slug
    assert f"results.{name} AS" in meta["executed_sql"]


def test_long_titles_are_truncated(telemetry_db, tmp_path):
    name = run(telemetry_db, tmp_path, "word " * 50)["result_table"]
    assert NAME.match(name) and len(name) <= 60


def test_same_title_in_same_second_gets_a_numeric_suffix(telemetry_db, tmp_path, monkeypatch):
    monkeypatch.setattr(queries, "_timestamp", lambda: "20261101_230402")
    a = run(telemetry_db, tmp_path, "Top ports")["result_table"]
    b = run(telemetry_db, tmp_path, "Top ports")["result_table"]
    c = run(telemetry_db, tmp_path, "Top ports")["result_table"]
    assert (a, b, c) == ("top_ports_20261101_230402", "top_ports_20261101_230402_2", "top_ports_20261101_230402_3")
    con = sqlite3.connect(tmp_path / "results.db")
    assert con.execute(f"SELECT COUNT(*) FROM {c}").fetchone()[0] > 0
