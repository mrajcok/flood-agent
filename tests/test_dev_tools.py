"""Dev mode: shift the Telemetry Database's timestamps so its latest record is the current time."""

import sqlite3

from flood_agent.dev_tools import shift_to_now
from flood_agent.semantic_layer import SemanticLayer

NOW = 1_800_000_000_000
LAYER = """\
dialect: sqlite
time_key: etime
tables:
  flows:
    columns:
      stime: {type: INTEGER, unit: epoch ms UTC}
      etime: {type: INTEGER, unit: epoch ms UTC}
      bytes: {type: INTEGER, unit: bytes}
"""


def layer_in(tmp_path):
    p = tmp_path / "layer.yaml"
    p.write_text(LAYER)
    return SemanticLayer(p)


def times(db):
    con = sqlite3.connect(db)
    try:
        return con.execute("SELECT stime, etime, bytes FROM flows ORDER BY etime").fetchall()
    finally:
        con.close()


def test_the_latest_record_ends_now_and_every_time_column_moves_by_the_same_amount(telemetry_db, tmp_path):
    before = times(telemetry_db)
    delta = shift_to_now(telemetry_db, layer_in(tmp_path), NOW)
    after = times(telemetry_db)
    assert delta == NOW - before[-1][1]
    assert after[-1][1] == NOW
    assert [(s - d, e - d, b) for s, e, b in after for d in [delta]] == before  # other columns untouched


def test_shifting_twice_gives_the_same_result(telemetry_db, tmp_path):
    layer = layer_in(tmp_path)
    shift_to_now(telemetry_db, layer, NOW)
    once = times(telemetry_db)
    assert shift_to_now(telemetry_db, layer, NOW) == 0
    assert times(telemetry_db) == once


def test_an_empty_database_is_left_alone(tmp_path):
    db = tmp_path / "empty.db"
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE flows (stime INTEGER, etime INTEGER)")
    con.commit()
    con.close()
    assert shift_to_now(db, layer_in(tmp_path), NOW) == 0
