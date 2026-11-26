"""Restricted Columns: which output columns of a query carry values from one (ADR 0008), seen through the query
runner both Cell kinds use. Each query runs for real, and the columns lineage says it read are checked against the
SQLite authorizer's read set."""

import sqlite3
from contextlib import closing
from pathlib import Path

import pytest
from conftest import databases_for_test

from flood_agent.catalog import Catalog, CellRun
from flood_agent.lineage import Known, Unknown, Unrestricted, restricted_columns
from flood_agent.queries import in_cidr, now_ms
from flood_agent.semantic_layer import SemanticLayer

pytestmark = pytest.mark.generated_data

REAL = Path(__file__).parent.parent / "semantic_layer.yaml"


def authorizer_reads(telemetry_db: Path, results_db: Path, sql: str) -> set[tuple[str, str]]:
    """The oracle: every (table, column) SQLite's authorizer reports reading for `sql`, with the statement cache
    off so that each statement is compiled, and so authorized, afresh."""
    reads: set[tuple[str, str]] = set()

    def record(action, table, column, database, source):
        if action == sqlite3.SQLITE_READ and database in ("main", "results") and column not in ("", "ROWID"):
            if not table.startswith("sqlite_"):
                reads.add((table.lower(), column.lower()))
        return sqlite3.SQLITE_OK

    with closing(sqlite3.connect(telemetry_db, cached_statements=0)) as con:
        con.execute("ATTACH DATABASE ? AS results", (str(results_db),))
        con.create_function("now", 0, now_ms)
        con.create_function("in_cidr", 2, in_cidr)
        for stand_in in ("SPLIT_PART", "REVERSE"):  # the runner's, which only need to exist here
            con.create_function(stand_in, -1, lambda *args: None)
        con.set_authorizer(record)
        con.execute(f"EXPLAIN {sql}")  # compiling is what authorizes; running could return millions of rows
    return reads


class Tables:
    """The query runner over a real Table Catalog and the real Semantic Layer."""

    def __init__(self, tmp_path: Path, telemetry_db: Path, query_runner):
        self.telemetry_db = telemetry_db
        databases = databases_for_test(tmp_path)
        self.results_db = databases.results_db
        layer = SemanticLayer(REAL)
        self.runner = query_runner(readable=lambda: self.catalog.readable(), schema=lambda n: self.catalog.schema(n))
        self.catalog = Catalog(databases.app_db, telemetry_tables=layer.telemetry_schemas(),
                               results_db=self.results_db, runner=self.runner)

    def run(self, sql: str) -> dict:
        result = self.runner.run(sql, max_rows=50)
        assert result["success"], result.get("raw_error")
        return result

    def make(self, sql: str) -> str:
        """Run `sql` and register its Result Table, as a Cell Run does."""
        result = self.run(sql)
        self.catalog.register_for(CellRun("cell", "run"), result["result_table"], columns=result["columns"],
                                  restricted_columns=restricted_columns(result["lineage"]))
        return result["result_table"]

    def restricted(self, sql: str) -> list[str]:
        """The output columns reported as restricted, for a query whose lineage is known; the columns lineage says
        were read are checked against the oracle."""
        outcome = self.run(sql)["lineage"]
        assert isinstance(outcome, Known), outcome
        assert outcome.sources == authorizer_reads(self.telemetry_db, self.results_db, sql)
        return list(outcome.restricted)


@pytest.fixture
def tables(tmp_path, telemetry_db, query_runner) -> Tables:
    return Tables(tmp_path, telemetry_db, query_runner)


def test_a_restricted_column_selected_as_it_is_stays_restricted(tables):
    assert tables.restricted("SELECT imsi, apn, etime FROM diameter") == ["imsi"]


@pytest.mark.parametrize("expression", [
    "SUBSTR(imsi, 1, 5)",
    "'id-' || msisdn",
    "CAST(imei AS INTEGER) + 1",
    "CASE WHEN apn IS NULL THEN imsi ELSE apn END",
    "CASE WHEN imsi LIKE '001%' THEN 'home' ELSE 'roamer' END",
    "COALESCE(apn, msisdn)",
    "SPLIT_PART(imsi, '0', 1)",
    "REVERSE(UPPER(imei))",
    "imsi = '001010000000000'",
])
def test_expressions_case_and_functions_carry_restricted_values(tables, expression):
    assert tables.restricted(f"SELECT {expression} AS derived, etime FROM diameter") == ["derived"]


def test_an_unnamed_expression_is_reported_under_the_name_the_result_table_gives_it(tables):
    assert tables.restricted("SELECT SUBSTR(imsi, 1, 5), apn FROM diameter") == ["SUBSTR(imsi, 1, 5)"]


@pytest.mark.parametrize("sql, restricted", [
    ("SELECT imsi AS id, apn AS imei FROM diameter", ["id"]),
    ("SELECT D.IMSI AS Id FROM Diameter AS D", ["Id"]),
    ('SELECT "imsi" AS "Subscriber id" FROM main.diameter;', ["Subscriber id"]),
    ("SELECT 1 AS imsi, imsi AS second FROM diameter", ["second"]),
    ("SELECT * FROM diameter", ["imsi", "msisdn", "imei"]),
    ("SELECT d.*, f.bytes FROM diameter d JOIN flows f ON f.sip = d.ue_ip_address", ["imsi", "msisdn", "imei"]),
    ("SELECT f.sip, d.imsi, d.msisdn AS phone FROM flows f LEFT JOIN diameter d ON d.ue_ip_address = f.sip",
     ["imsi", "phone"]),
    ("SELECT a.imsi, b.imsi FROM diameter a JOIN diameter b ON a.session_id = b.session_id",
     ["imsi", "imsi:1"]),
    ("SELECT imsi, a.apn FROM diameter a JOIN diameter b USING (imsi)", ["imsi"]),
])
def test_aliases_stars_and_joins(tables, sql, restricted):
    assert tables.restricted(sql) == restricted


@pytest.mark.parametrize("sql, restricted", [
    ("WITH s AS (SELECT imsi AS id, apn FROM diameter) SELECT id AS who, apn FROM s", ["who"]),
    ("WITH s(a, b) AS (SELECT imsi, apn FROM diameter), t AS (SELECT * FROM s) SELECT * FROM t", ["a"]),
    ("SELECT x.id, x.apn FROM (SELECT msisdn AS id, apn FROM diameter) AS x", ["id"]),
    ("SELECT (SELECT imsi FROM diameter d WHERE d.ue_ip_address = f.sip LIMIT 1) AS who, f.sip FROM flows f", ["who"]),
    ("SELECT sip AS id, dip FROM flows UNION SELECT imsi, apn FROM diameter", ["id"]),
    ("SELECT imsi AS id FROM diameter UNION ALL SELECT sip FROM flows", ["id"]),
    ("SELECT sip FROM flows EXCEPT SELECT imsi FROM diameter", ["sip"]),
    ("WITH u(id) AS (SELECT apn FROM diameter UNION SELECT imei FROM diameter) SELECT id FROM u", ["id"]),
    ("WITH s AS (SELECT imsi, apn FROM diameter) SELECT apn FROM s", []),
])
def test_ctes_subqueries_and_set_operations(tables, sql, restricted):
    assert tables.restricted(sql) == restricted


@pytest.mark.parametrize("sql", [
    "SELECT sip, dip FROM diameter WHERE imsi = '001010000000000'",
    "SELECT f.sip FROM flows f JOIN diameter d ON d.imsi = f.circuit",
    "SELECT apn, SUM(latency_ms) AS ms FROM diameter GROUP BY apn, imsi HAVING MIN(msisdn) > '0' ORDER BY MAX(imei)",
    "SELECT apn AS name, COUNT(imsi) AS n FROM diameter GROUP BY name HAVING n > 0 ORDER BY n, MIN(imsi)",
    "SELECT ROW_NUMBER() OVER (PARTITION BY imsi ORDER BY msisdn) AS n, apn FROM diameter",
    "SELECT SUM(latency_ms) OVER (PARTITION BY imsi) AS ms FROM diameter ORDER BY imsi",
    "SELECT SUM(latency_ms) FILTER (WHERE imsi > '0') AS ms FROM diameter",
    "SELECT GROUP_CONCAT(apn, ',' ORDER BY imsi) AS apns FROM diameter",
    "SELECT sip FROM flows WHERE sip IN (SELECT imsi FROM diameter)",
])
def test_a_restricted_column_read_only_to_filter_group_or_order_leaves_the_table_unrestricted(tables, sql):
    assert tables.restricted(sql) == []


@pytest.mark.parametrize("sql, restricted", [
    ("SELECT apn, COUNT(DISTINCT imsi) AS subscribers FROM diameter GROUP BY apn", []),
    ("SELECT COUNT(imsi) + 1 AS n, COUNT(CASE WHEN imei IS NULL THEN msisdn END) AS m FROM diameter", []),
    ("SELECT MIN(imsi) AS first, MAX(msisdn) AS last, COUNT(*) AS n FROM diameter", ["first", "last"]),
    ("SELECT GROUP_CONCAT(imsi, ',') AS ids FROM diameter", ["ids"]),
    ("SELECT GROUP_CONCAT(imsi ORDER BY apn) AS ids FROM diameter", ["ids"]),
    ("SELECT FIRST_VALUE(imsi) OVER (ORDER BY etime) AS first FROM diameter", ["first"]),
    ("SELECT MIN(imsi) OVER (PARTITION BY apn) AS first FROM diameter", ["first"]),
    ("SELECT s.n, s.first FROM (SELECT COUNT(imsi) AS n, MIN(imsi) AS first FROM diameter) AS s", ["first"]),
])
def test_counts_carry_nothing_but_min_max_and_string_aggregates_do(tables, sql, restricted):
    assert tables.restricted(sql) == restricted


def test_a_result_table_built_from_a_restricted_one_inherits_only_the_restricted_column(tables):
    first = tables.make("SELECT imsi AS id, apn FROM diameter")
    assert tables.restricted(f"SELECT SUBSTR(id, 1, 5) AS prefix, apn FROM {first}") == ["prefix"]
    assert tables.restricted(f"SELECT apn FROM {first}") == []
    second = tables.make(f"SELECT * FROM results.{first.upper()}")
    assert tables.restricted(f"SELECT apn, id FROM {second}") == ["id"]


def test_a_renamed_table_is_read_under_its_new_name(tables):
    first = tables.make("SELECT imsi AS id, apn FROM diameter")
    tables.catalog.update(first, "subscribers")
    assert tables.restricted("SELECT id, apn FROM subscribers") == ["id"]
    assert tables.catalog["subscribers"].restricted_columns == ("id",)


def test_columns_named_by_the_database_can_be_read_from_a_result_table(tables):
    first = tables.make("SELECT SUBSTR(imsi, 1, 5), apn FROM diameter")
    assert tables.restricted(f'SELECT "SUBSTR(imsi, 1, 5)" AS p, apn FROM {first}') == ["p"]
    assert tables.restricted(f"SELECT * FROM {first}") == ["SUBSTR(imsi, 1, 5)"]


def test_every_column_of_a_result_table_with_unknown_lineage_counts_as_restricted(tables):
    first = tables.make('SELECT apn, "no such column" AS text FROM diameter')
    assert tables.catalog[first].lineage_unknown and tables.catalog[first].restricted
    assert tables.restricted(f"SELECT apn AS a, COUNT(text) AS n FROM {first} GROUP BY apn") == ["a"]


def test_a_lookup_table_is_never_restricted(tables):
    tables.catalog.add_lookup("watched", "imsi,note\n001010000000000,x")
    assert tables.run("SELECT * FROM lookup_watched")["lineage"] == Unrestricted()
    assert tables.restricted("SELECT w.imsi, d.msisdn FROM diameter d JOIN lookup_watched w ON w.imsi = d.imsi") == [
        "msisdn"]


@pytest.mark.parametrize("sql", [
    "SELECT `imsi` FROM diameter",  # SQLite's own quoting, which does not parse
    "SELECT [imsi] FROM diameter",  # and this parses as an array
    'SELECT "no such column" FROM diameter',  # SQLite reads this as a string
    "SELECT rowid, apn FROM diameter",
    "SELECT d.imsi, j.value FROM diameter d, json_each('[1]') j",  # a table function
    "SELECT apn IN lookup_one AS found FROM diameter",  # a table read without FROM
    "WITH RECURSIVE n(i) AS (SELECT 1 UNION ALL SELECT i + 1 FROM n WHERE i < 3) SELECT i, imsi FROM n, diameter",
    # SQLite reads the later CTE here, not the table of that name
    "WITH a AS (SELECT * FROM lookup_one), lookup_one AS (SELECT imsi AS ip FROM diameter) SELECT ip FROM a",
    "WITH diameter AS (SELECT 1 AS imsi) SELECT m.imsi, d.imsi FROM main.diameter AS m, diameter AS d",
])
def test_lineage_that_cannot_be_worked_out_for_restricted_data_is_unknown(tables, sql):
    tables.catalog.add_lookup("one", "ip\n10.0.0.1")
    assert tables.run(sql)["lineage"] == Unknown()


@pytest.mark.parametrize("word", ["current_user", "count(apn)"])
def test_a_restricted_column_with_a_name_that_reads_as_something_else_is_not_missed(tables, word):
    first = tables.make(f'SELECT imsi AS "{word}" FROM diameter')
    quoted = f"[{word}]" if "(" in word else word  # both read the column in SQLite
    assert tables.run(f"SELECT {quoted} AS x FROM {first}")["lineage"] == Unknown()


@pytest.mark.parametrize("sql", [
    "SELECT * FROM flows",
    'SELECT "no such column" FROM flows',
    "SELECT rowid FROM flows",
    "WITH RECURSIVE n(i) AS (SELECT 1 UNION ALL SELECT i + 1 FROM n WHERE i < 3) SELECT i, sip FROM n, flows",
    "SELECT question FROM dns_metadata",
    "SELECT 1 AS imsi",
])
def test_a_query_reading_no_restricted_columns_is_unrestricted_even_when_lineage_would_fail(tables, sql):
    assert tables.run(sql)["lineage"] == Unrestricted()


def test_a_query_that_does_not_parse_is_unknown_as_its_tables_cannot_be_listed(tables):
    assert tables.run("SELECT `sip` FROM flows")["lineage"] == Unknown()
