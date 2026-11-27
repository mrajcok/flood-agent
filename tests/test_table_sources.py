"""The record of where a query's rows came from, seen through the query runner both Cell kinds use, for what HTTP
cannot produce: Telemetry Database objects CANOPY does not ship, unregistered results tables and failures. The
Telemetry Database here holds a handful of made-up rows."""

import sqlite3
from contextlib import closing
from pathlib import Path

import pytest
from conftest import databases_for_test

from flood_agent import table_sources


@pytest.fixture
def telemetry_db(tmp_path: Path) -> Path:
    path = tmp_path / "telemetry.db"
    with closing(sqlite3.connect(path)) as con:
        con.executescript("""
            CREATE TABLE subscribers (id INTEGER, imsi TEXT, apn TEXT);
            CREATE INDEX subscribers_apn ON subscribers (apn);
            CREATE INDEX subscribers_prefix ON subscribers (substr(imsi, 1, 3)) WHERE apn IS NOT NULL;
            CREATE TABLE "Cells" (id INTEGER PRIMARY KEY, site TEXT);
            CREATE TABLE tariffs (code TEXT PRIMARY KEY, price INTEGER) WITHOUT ROWID;
            CREATE VIEW networks AS SELECT apn FROM subscribers;
            CREATE VIEW one_each AS SELECT 1 AS one FROM subscribers;
            CREATE VIEW constant AS SELECT 7 AS seven;
            INSERT INTO subscribers VALUES (1, '001010000000001', 'internet');
            ANALYZE;
        """)
    return path


@pytest.fixture
def results_db(tmp_path: Path) -> Path:
    """The test User's results database, holding tables made up here."""
    path = databases_for_test(tmp_path).results_db
    with closing(sqlite3.connect(path)) as con, con:
        for table in ("rt_cells", "rt_subscribers", "rt_unknown", "lookup_ips"):
            con.execute(f"CREATE TABLE {table} (id INTEGER)")
    return path


RECORDS = {
    "rt_cells": frozenset({"cells"}), "rt_subscribers": frozenset({"subscribers"}), "rt_unknown": None,
    "lookup_ips": frozenset(),
}


@pytest.fixture
def sources(query_runner, results_db):
    """`sources(sql)`: the record the query runner makes for the table of `sql`'s rows."""
    runner = query_runner(set(RECORDS), recorded=RECORDS.get)

    def run(sql: str):
        result = runner.run(sql, 10)
        assert result["success"], result.get("raw_error")
        return result["sources"]

    return run


@pytest.mark.parametrize("sql, expected", [
    ("SELECT price FROM tariffs", {"tariffs"}),  # a table without rowids
    ("SELECT COUNT(*) + 0 AS n FROM CELLS", {"cells"}),  # made as "Cells"
    ("SELECT id FROM subscribers WHERE substr(imsi, 1, 3) = '001' AND apn IS NOT NULL", {"subscribers"}),
    # SQLite leaves the joined table out of its program, so that only the authorizer speaks of it
    ("SELECT s.apn FROM subscribers s LEFT JOIN cells ON cells.id = s.id", {"subscribers", "cells"}),
    ("SELECT r.id FROM rt_cells r JOIN lookup_ips USING (id) JOIN rt_subscribers USING (id)",
     {"cells", "subscribers"}),
    ("SELECT COUNT(*) + 0 AS n FROM results.RT_SUBSCRIBERS", {"subscribers"}),
])
def test_the_record_names_the_telemetry_tables_the_rows_came_from(sources, sql, expected):
    assert sources(sql) == expected


@pytest.mark.parametrize("sql", [
    "SELECT apn FROM networks",
    "SELECT COUNT(*) + 0 AS n FROM networks",
    "SELECT one FROM one_each",
    "SELECT COUNT(*) + 0 AS n FROM Networks",
])
def test_a_view_counts_the_table_under_it(sources, sql):
    assert "subscribers" in sources(sql)


def test_a_view_of_no_table_is_recorded_by_its_own_name(sources):
    assert sources("SELECT seven FROM constant") == {"constant"}  # which no Semantic Layer need describe


@pytest.mark.parametrize("sql", [
    "SELECT stat FROM sqlite_stat1",
    "SELECT COUNT(*) + 0 AS n FROM sqlite_stat1",
    "SELECT c.site FROM cells c, rt_unknown",
    # A results table that is in no record (here as a replacement's temporary one is), only its rows being counted
    "SELECT COUNT(*) + 0 AS n FROM RESULTS.rt_temporary",
])
def test_anything_unrecognised_makes_the_record_unknown(sources, results_db, sql):
    with closing(sqlite3.connect(results_db)) as con, con:
        con.execute("CREATE TABLE rt_temporary (id INTEGER)")
    assert sources(sql) is None


def test_a_cursor_of_an_unfamiliar_kind_makes_the_record_unknown(sources, monkeypatch):
    monkeypatch.setattr(table_sources, "_TRANSIENT_OPENS", frozenset())  # as if SQLite had added one
    assert sources("SELECT DISTINCT site FROM cells") is None


def test_a_failure_while_establishing_the_record_makes_it_unknown_and_keeps_the_rows(query_runner, results_db):
    def fail(name: str):
        raise sqlite3.OperationalError("database is locked")

    result = query_runner({"rt_cells"}, recorded=fail).run("SELECT id FROM rt_cells", 10)
    assert result["success"] and result["sources"] is None


def test_a_results_table_named_like_a_telemetry_table_is_not_taken_for_it(query_runner, results_db):
    with closing(sqlite3.connect(results_db)) as con, con:
        con.execute("CREATE TABLE cells (id INTEGER)")  # in no record, and named like a Telemetry table
    runner = query_runner({"cells"}, recorded=RECORDS.get)
    assert runner.run("SELECT id FROM results.cells", 10)["sources"] is None
    assert runner.run("SELECT site FROM cells", 10)["sources"] == {"cells"}  # SQLite takes the Telemetry one
