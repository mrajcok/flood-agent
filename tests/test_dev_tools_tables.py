"""The dev time shift moves every Telemetry table by one delta."""

import shutil
import sqlite3
from pathlib import Path

from flood_agent.dev_tools import shift_to_now
from flood_agent.semantic_layer import SemanticLayer

REAL = Path(__file__).parent.parent / "semantic_layer.yaml"
NOW = 1_800_000_000_000
TWO = """\
dialect: sqlite
time_key: etime
tables:
  flows:
    columns:
      stime: {type: INTEGER, unit: epoch ms UTC}
      etime: {type: INTEGER, unit: epoch ms UTC}
  dns:
    columns:
      etime: {type: INTEGER, unit: epoch ms UTC}
  seen_table:
    time_key: seen
    columns:
      seen: {type: INTEGER}
"""


def test_the_three_shipped_telemetry_tables_move_by_one_delta(generated_db, tmp_path):
    db = Path(shutil.copy(generated_db, tmp_path / "telemetry.db"))

    def bounds():
        con = sqlite3.connect(db)
        try:
            return {t: con.execute(f"SELECT MIN(etime), MAX(etime) FROM {t}").fetchone()
                    for t in ("flows", "dns_metadata", "diameter")}
        finally:
            con.close()

    before = bounds()
    delta = shift_to_now(db, SemanticLayer(REAL), NOW)
    assert delta == NOW - max(hi for _, hi in before.values())
    assert bounds() == {t: (lo + delta, hi + delta) for t, (lo, hi) in before.items()}


def test_every_table_moves_by_one_delta_from_the_latest_time_key_overall(tmp_path):
    db = tmp_path / "two.db"
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE flows (stime INTEGER, etime INTEGER)")
    con.execute("CREATE TABLE dns (etime INTEGER)")
    con.execute("CREATE TABLE seen_table (seen INTEGER)")
    con.execute("INSERT INTO flows VALUES (100, 200)")
    con.execute("INSERT INTO dns VALUES (500)")  # the latest overall
    con.execute("INSERT INTO seen_table VALUES (300)")
    con.commit()
    con.close()
    p = tmp_path / "two.yaml"
    p.write_text(TWO)
    delta = shift_to_now(db, SemanticLayer(p), NOW)
    assert delta == NOW - 500
    con = sqlite3.connect(db)
    assert con.execute("SELECT stime, etime FROM flows").fetchall() == [(100 + delta, 200 + delta)]
    assert con.execute("SELECT etime FROM dns").fetchall() == [(NOW,)]
    assert con.execute("SELECT seen FROM seen_table").fetchall() == [(300 + delta,)]
    con.close()
