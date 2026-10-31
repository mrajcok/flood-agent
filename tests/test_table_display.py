"""Table Display polish at the HTTP seam: pagination, sorting, CSV, format metadata."""

import csv
import io
import shutil
import subprocess
from pathlib import Path

import pytest
from conftest import ScriptedLLM, query_limits

from flood_defender import queries

LAYER = """\
dialect: sqlite
window_end: 1600000100000
tables:
  flows:
    columns:
      proto: {type: INTEGER, enum: {6: TCP, 17: UDP, 1: ICMP}}
      tcp_flags: {type: INTEGER, enum_bits: {1: FIN, 2: SYN, 4: RST, 8: PSH, 16: ACK, 32: URG}}
      etime: {type: INTEGER, unit: epoch ms UTC}
      bytes: {type: INTEGER, unit: bytes}
      pkts: {type: INTEGER, unit: packets}
      dport: {type: INTEGER}
"""

GEN = (
    "WITH RECURSIVE n(i) AS (SELECT 1 UNION ALL SELECT i+1 FROM n WHERE i<120) "
    "SELECT i AS k, (i * 37) % 101 AS bytes, 6 AS proto, 'x' || i AS label, "
    "1700000000000 + i AS etime, i AS total_pkts FROM n"
)


@pytest.fixture
def env(build_agent, web_client, semantic_layer_path):
    semantic_layer_path.write_text(LAYER)
    agent = build_agent(ScriptedLLM())
    client = web_client(agent)

    def table(sql=GEN):
        meta = queries.run_query(agent.config.telemetry_db, agent.config.results_db, sql, **query_limits())
        assert meta["success"], meta
        return meta["result_table"]

    return client, table


def test_pagination_boundaries(env):
    client, table = env
    rt = table()
    base = f"/api/results/{rt}/rows"
    first = client.get(base).json()
    assert first["total"] == 120 and first["page"] == 1 and first["page_size"] == 50
    assert len(first["rows"]) == 50
    last = client.get(base, params={"page": 3}).json()
    assert len(last["rows"]) == 20 and last["rows"][-1][0] == 120
    beyond = client.get(base, params={"page": 4}).json()
    assert beyond["rows"] == [] and beyond["total"] == 120
    exact = client.get(base, params={"page": 2, "page_size": 60}).json()
    assert len(exact["rows"]) == 60 and exact["rows"][-1][0] == 120
    assert client.get(base, params={"page": 0}).status_code == 422
    assert client.get(base, params={"page_size": 0}).status_code == 422
    assert client.get(base, params={"page_size": 100000}).status_code == 422


def test_sort_applies_across_pages(env):
    client, table = env
    rt = table()
    base = f"/api/results/{rt}/rows"
    seen = []
    for page in (1, 2, 3):
        d = client.get(base, params={"page": page, "page_size": 50, "sort": "bytes", "dir": "desc"}).json()
        seen += [r[d["columns"].index("bytes")] for r in d["rows"]]
    assert len(seen) == 120 and seen == sorted(seen, reverse=True)
    asc = client.get(base, params={"sort": "k", "dir": "asc"}).json()
    assert [r[0] for r in asc["rows"]][:3] == [1, 2, 3]
    desc = client.get(base, params={"sort": "k", "dir": "desc"}).json()
    assert desc["rows"][0][0] == 120
    # Ties are broken deterministically, so repeated calls agree.
    a = client.get(base, params={"sort": "proto"}).json()
    b = client.get(base, params={"sort": "proto"}).json()
    assert a == b


@pytest.mark.parametrize(
    "bad",
    ['k; DROP TABLE x', 'k" --', "nope", "bytes, k", "(SELECT 1)", "k DESC", "rowid", ""],
)
def test_sort_rejects_unknown_columns_and_injection(env, bad):
    client, table = env
    rt = table()
    r = client.get(f"/api/results/{rt}/rows", params={"sort": bad})
    assert r.status_code == 400
    assert client.get(f"/api/results/{rt}/rows").status_code == 200  # table intact


def test_sort_dir_validated(env):
    client, table = env
    rt = table()
    r = client.get(f"/api/results/{rt}/rows", params={"sort": "k", "dir": "desc; DROP"})
    assert r.status_code in (400, 422)


def test_csv_returns_full_table_with_escaping_and_formula_neutralised(env):
    client, table = env
    rt = table(
        "SELECT dport AS \"a,b\", 'he said \"hi\"' AS q, 'line1\nline2' AS nl, "
        "'=SUM(A1)' AS f1, '+1' AS f2, '-2+3' AS f3, '@x' AS f4, 'ok' AS f5, -5 AS neg, NULL AS nul "
        "FROM flows"
    )
    r = client.get(f"/api/results/{rt}/csv")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/csv")
    assert f"{rt}.csv" in r.headers["content-disposition"]
    parsed = list(csv.reader(io.StringIO(r.text, newline="")))
    assert parsed[0] == ["a,b", "q", "nl", "f1", "f2", "f3", "f4", "f5", "neg", "nul"]
    assert len(parsed) == 4  # header + all 3 flows (not just one page)
    row = parsed[1]
    assert row[1] == 'he said "hi"' and row[2] == "line1\nline2"
    assert row[3:7] == ["'=SUM(A1)", "'+1", "'-2+3", "'@x"] and row[7] == "ok"
    assert row[8] == "-5" and row[9] == ""  # real numbers are not mangled


def test_csv_is_full_even_beyond_page_size_and_unknown_is_404(env):
    client, table = env
    rt = table()
    rows = list(csv.reader(io.StringIO(client.get(f"/api/results/{rt}/csv").text)))
    assert len(rows) == 121
    assert client.get("/api/results/rt_deadbeef/csv").status_code == 404
    assert client.get("/api/results/nope/csv").status_code == 404


def test_formats_come_from_semantic_layer_by_shared_name(env):
    client, table = env
    rt = table(
        "SELECT proto, tcp_flags, etime, bytes, pkts, dport, bytes AS bytes_total, "
        "etime AS bucket FROM flows"
    )
    f = client.get(f"/api/results/{rt}/rows").json()["formats"]
    assert f["proto"] == {"kind": "enum", "map": {"6": "TCP", "17": "UDP", "1": "ICMP"}}
    assert f["tcp_flags"]["kind"] == "flags"
    assert f["tcp_flags"]["map"] == {"1": "FIN", "2": "SYN", "4": "RST", "8": "PSH", "16": "ACK", "32": "URG"}
    assert f["etime"] == {"kind": "time"}
    assert f["bytes"] == {"kind": "bytes"}
    for plain in ("pkts", "dport", "bytes_total", "bucket"):
        assert f[plain] == {"kind": "plain"}


def test_js_formatting_functions():
    if not shutil.which("node"):
        pytest.skip("node not installed")
    test_file = Path(__file__).parent / "js" / "format.test.js"
    r = subprocess.run(["node", "--test", str(test_file)], capture_output=True, text=True)
    assert r.returncode == 0, r.stdout + r.stderr
