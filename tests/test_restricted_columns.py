"""Restricted Columns: which output columns of a query carry values from one (ADR 0008), seen through the query
runner both Cell kinds use. Each query runs for real, and the columns lineage says it read are checked against the
SQLite authorizer's read set."""

import re
import sqlite3
from contextlib import closing
from pathlib import Path

import pytest
import yaml
from conftest import databases_for_test
from sqlglot.dialects.postgres import Postgres

from flood_agent import sqlite_sql
from flood_agent.catalog import Catalog, CellRun
from flood_agent.lineage import Known, Unknown, Unrestricted, restriction
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
                                  restriction=restriction(result["lineage"]))
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
    assert tables.catalog["subscribers"].restriction.columns == ("id",)


def test_columns_named_by_the_database_can_be_read_from_a_result_table(tables):
    first = tables.make("SELECT SUBSTR(imsi, 1, 5), apn FROM diameter")
    assert tables.restricted(f'SELECT "SUBSTR(imsi, 1, 5)" AS p, apn FROM {first}') == ["p"]
    assert tables.restricted(f"SELECT * FROM {first}") == ["SUBSTR(imsi, 1, 5)"]


def test_every_column_of_a_result_table_with_unknown_lineage_counts_as_restricted(tables):
    first = tables.make('SELECT apn, "no such column" AS text FROM diameter')
    assert tables.catalog[first].restriction.unknown and tables.catalog[first].restricted
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


def test_a_restricted_column_named_like_a_call_is_not_missed_between_square_brackets(tables):
    first = tables.make('SELECT imsi AS "count(apn)" FROM diameter')
    assert tables.run(f"SELECT [count(apn)] AS x FROM {first}")["lineage"] == Unknown()  # SQLite reads the column


@pytest.mark.parametrize("sql", [
    "SELECT * FROM flows",
    'SELECT "no such column" FROM flows',
    "SELECT rowid FROM flows",
    "WITH RECURSIVE n(i) AS (SELECT 1 UNION ALL SELECT i + 1 FROM n WHERE i < 3) SELECT i, sip FROM n, flows",
    "SELECT question FROM dns_metadata",
    "SELECT 1 AS imsi",
    # Syntax the parser does not follow: SQLite alone says which tables are read
    "SELECT `sip` FROM flows",
    "SELECT [sip] FROM [flows]",
    "SELECT sip AS 'Gr\u00f6\u00dfe' FROM flows",
    "SELECT sip AS offset FROM flows NATURAL JOIN dns_metadata",
    "SELECT /* /* */ sip FROM flows -- */",
    "SELECT date 'zz' FROM (SELECT sip AS date FROM flows)",
])
def test_a_query_reading_no_restricted_columns_is_unrestricted_even_when_lineage_would_fail(tables, sql):
    assert tables.run(sql)["lineage"] == Unrestricted()


# SQL that SQLite and the parser read differently. Each of these was classified as carrying nothing restricted.
@pytest.mark.parametrize("sql, restricted", [
    # Block comments do not nest in SQLite
    ("SELECT sip FROM flows /* /* */ UNION SELECT imsi FROM diameter -- */", ["sip"]),
    ("SELECT /* /* */ imsi AS x FROM diameter -- */ sip AS x FROM diameter", ["x"]),
    # A carriage return does not end a line comment in SQLite
    ("SELECT sip FROM flows --\r/*\n UNION SELECT imsi FROM diameter --*/", ["sip"]),
])
def test_comments_are_read_as_sqlite_reads_them(tables, sql, restricted):
    assert tables.restricted(sql) == restricted


@pytest.mark.parametrize("word", [
    "date", "e", "n", "time", "timestamp", "interval", "text", "int", "json", "uuid", "decimal", "bit", "inet",
])
def test_a_word_and_a_string_are_a_column_and_its_alias_not_a_typed_literal(tables, word):
    assert tables.restricted(f"""SELECT {word} 'zz' FROM (SELECT imsi AS "{word}" FROM diameter)""") == ["zz"]


@pytest.mark.parametrize("word", [
    "current_user", "session_user", "user", "localtime", "localtimestamp", "current_schema", "current_catalog",
    "sysdate", "current_role",
])
def test_a_word_that_is_a_function_elsewhere_is_a_column_in_sqlite(tables, word):
    assert tables.restricted(f'SELECT {word} FROM (SELECT imsi AS "{word}" FROM diameter)') == [word]


def test_names_differing_only_in_the_case_of_a_non_ascii_letter_are_not_confused(tables):
    sql = ('WITH "é" AS (SELECT imsi AS v FROM diameter), "É" AS (SELECT sip AS v FROM diameter) '
           'SELECT v FROM "é"')
    assert tables.run(sql)["lineage"] == Unknown()


# Shapes outside the ones whose names SQLite and the parser are sure to read alike.
@pytest.mark.parametrize("sql", [
    # One name for two things
    "SELECT d.apn FROM diameter d, (SELECT d.ip FROM lookup_one d) s",
    "SELECT diameter.ip FROM lookup_one AS diameter, diameter AS d",
    "SELECT a.apn FROM (SELECT apn FROM diameter) a UNION ALL SELECT a.sip FROM flows a",
    "SELECT x.apn FROM (diameter AS d JOIN lookup_one AS l ON 1) AS x",
    # A column qualified with its database
    "SELECT main.diameter.apn FROM diameter",
    # A subquery or CTE column without a plain name of its own
    "SELECT n.apn FROM (SELECT apn, 1 + 1 FROM diameter) AS n",
    "WITH s AS (SELECT apn, apn FROM diameter) SELECT 1 AS one FROM s",
    'SELECT s.apn FROM (SELECT apn, ue_ip_address AS "x:1" FROM diameter) s',
    # A reference to an outer query without a qualifier, or from a FROM subquery or a CTE
    "SELECT (SELECT max(bytes) FROM flows WHERE sip = ue_ip_address) AS b FROM diameter",
    "SELECT apn AS q FROM diameter WHERE EXISTS (SELECT 1 FROM flows WHERE sip = q)",
    "SELECT (SELECT s.v FROM (SELECT d.apn AS v) s) AS o FROM diameter d",
    "SELECT (WITH c AS (SELECT d.apn AS v) SELECT c.v FROM c) AS o FROM diameter d",
    # Joins that name columns for the query
    "SELECT d.apn FROM diameter d NATURAL JOIN lookup_one",
    "SELECT d.apn FROM diameter d RIGHT JOIN lookup_one USING (imsi)",
    # A subquery that decides how many rows there are
    "SELECT sip FROM flows LIMIT (SELECT max(imsi) FROM diameter) % 10",
    "SELECT sip FROM flows LIMIT 10 OFFSET (SELECT max(imsi) FROM diameter) % 10",
    "WITH c AS (SELECT count(*) AS n FROM diameter WHERE imsi > '') SELECT sip FROM flows LIMIT (SELECT n FROM c)",
])
def test_a_query_whose_names_might_be_read_two_ways_is_unknown(tables, sql):
    tables.catalog.add_lookup("one", "ip,imsi\n10.0.0.1,001010000000000")
    assert tables.run(sql)["lineage"] == Unknown()


@pytest.mark.parametrize("sql", [
    # One alias used twice for one table, as in the two halves of a UNION
    "SELECT f.sip FROM flows f JOIN diameter d ON d.imsi = f.circuit "
    "UNION ALL SELECT f.dip FROM flows f JOIN diameter d ON d.imsi = f.circuit",
    # A correlated subquery that says which query it reads
    "SELECT (SELECT count(*) FROM flows f WHERE f.sip = d.ue_ip_address) AS n, d.apn FROM diameter d",
    # A name a subquery's own sources have is the subquery's own, though an outer query has it too
    "SELECT apn, (SELECT max(apn) FROM diameter WHERE result_code = 3004) AS worst FROM diameter",
    # A query's own output names, in the clauses that only filter, group and order
    "WITH s(network, n) AS (SELECT apn, count(*) AS total FROM diameter GROUP BY apn HAVING total > 0 "
    "ORDER BY total) SELECT network, n FROM s",
])
def test_ordinary_queries_stay_known(tables, sql):
    assert tables.restricted(sql) == []


def shipped_queries() -> list:
    """Every query CANOPY ships as an example of what to ask."""
    from flood_agent.evalset.golden import load_golden
    layer = yaml.safe_load(REAL.read_text())
    metrics = [(name, m["sql"]) for name, m in layer["metrics"].items() if m["sql"].startswith(("SELECT", "WITH"))]
    golden = [(q.id, q.sql) for q in load_golden()]
    cells = [(f"{q.id}-above", cell["sql"]) for q in load_golden() for cell in q.above or [] if cell["kind"] == "sql"]
    prompts = yaml.safe_load((REAL.parent / "src/flood_agent/canned_prompts.yaml").read_text())
    canned = [(f"{prompt['id']}-{n}", call["args"]["sql"])
              for prompt in prompts if not {"failed_tool_calls", "notice"} & prompt["expect"].keys()
              for n, call in enumerate(c for step in prompt["steps"] for c in step.get("tool_calls", []))
              if "{" not in call.get("args", {}).get("sql", "{")]
    return [pytest.param(sql.replace("<start>", "0").replace("<end>", "2000000000000"), id=name)
            for name, sql in metrics + golden + cells + canned]


@pytest.mark.parametrize("sql", shipped_queries())
def test_the_lineage_of_every_shipped_query_is_worked_out(tables, sql):
    tables.catalog.add_lookup("watchlist", "ip\n10.0.0.1")
    assert tables.run(sql)["lineage"] != Unknown()


def identifiers(telemetry_db: Path) -> set[str]:
    """Every value of a Restricted Column in the test Telemetry Database: what no unrestricted column may hold."""
    with closing(sqlite3.connect(telemetry_db)) as con:
        return {value for column in ("imsi", "msisdn", "imei")
                for value, in con.execute(f"SELECT DISTINCT {column} FROM diameter WHERE {column} IS NOT NULL")}


@pytest.fixture
def leaks(tables):
    """`leaks(sql)`: the output columns that hold an identifier although the query's lineage calls them unrestricted.
    None where SQLite itself refuses the SQL."""
    secret = identifiers(tables.telemetry_db)

    def find(sql: str) -> list[str] | None:
        result = tables.runner.run(sql, max_rows=5)
        if not result["success"]:
            return None
        outcome, rows = result["lineage"], tables.runner.fetch_rows(result["result_table"])
        tables.runner.drop(result["result_table"])
        if isinstance(outcome, Unknown):
            return []
        restricted = outcome.restricted if isinstance(outcome, Known) else ()
        return [column for i, column in enumerate(rows["columns"]) if column not in restricted
                and any(secret.intersection(re.findall("[0-9]+", str(row[i]))) for row in rows["rows"])]

    return find


# One query per way the parser and SQLite could part company; each puts identifiers in an output column.
@pytest.mark.parametrize("sql", [
    # A join with USING, whose columns SQLite's authorizer does not always report
    "SELECT imsi FROM lookup_one FULL JOIN diameter USING (imsi)",
    "SELECT imsi AS v FROM diameter FULL JOIN lookup_one USING (imsi)",
    "SELECT * FROM lookup_one NATURAL FULL JOIN diameter",
    "WITH s(p, q) AS (SELECT * FROM (SELECT 1 AS a, imsi AS k FROM diameter) JOIN lookup_one USING (k)) "
    "SELECT p, q FROM s",
    # Names SQLite makes from an expression's text, which the parser would look for elsewhere
    'SELECT (SELECT "likely(imsi)" FROM (SELECT likely(imsi) FROM diameter)) AS v '
    'FROM (SELECT sip AS "likely(imsi)" FROM flows)',
    'SELECT (SELECT "+imsi" FROM (SELECT +imsi FROM diameter)) AS v FROM (SELECT sip AS "+imsi" FROM flows)',
    'SELECT (SELECT "CAST(imsi AS TEXT)" FROM (SELECT CAST(imsi AS TEXT) FROM diameter)) AS v '
    'FROM (SELECT sip AS "CAST(imsi AS TEXT)" FROM flows)',
    "SELECT 'x' AS \"max(imsi)\", \"max(imsi)\" AS v FROM (SELECT max(imsi) FROM diameter)",
    'SELECT (SELECT "_col_0" FROM (SELECT 1 + 1 FROM flows)) AS v FROM (SELECT imsi AS "_col_0" FROM diameter)',
    # An alias that has the name of an outer column
    "SELECT (SELECT w FROM (SELECT sip AS q, q AS w FROM flows)) AS v FROM (SELECT imsi AS q FROM diameter)",
    "SELECT (SELECT max(w) FROM (SELECT q AS w FROM (SELECT 1 AS k) WHERE 'x' <> q)) AS v "
    "FROM (SELECT imsi AS q FROM diameter)",
    # Comments, white space and statement ends
    "SELECT imsi FROM diameter /* open",
    "SELECT/**/imsi/*/ */AS x FROM diameter",
    "SELECT imsi--x\r, sip\nAS x FROM diameter",
    "SELECT\fimsi\tAS\r\nx FROM diameter;-- ;",
    "SELECT\x0bimsi FROM diameter",
    "SELECT\u00a0imsi FROM diameter",
    "SELECT imsi AS x\u00a0FROM FROM diameter",
    "\ufeffSELECT imsi FROM diameter",
    "SELECT imsi\x00 FROM diameter",
    "SELECT imsi AS x\u2028 FROM diameter",
    # Quoting, and strings where names go
    "SELECT `imsi` AS v FROM diameter",
    "SELECT [imsi] AS v FROM [diameter]",
    "SELECT imsi 'v' FROM diameter",
    "SELECT v FROM (SELECT imsi 'v' FROM 'diameter')",
    "SELECT d.'imsi' AS v FROM diameter d",
    "SELECT '--' AS a, imsi AS b FROM diameter",
    "SELECT '\\' AS a, imsi AS b, '\\' AS c FROM diameter",
    "SELECT E'\\' AS a, imsi AS b FROM (SELECT 1 AS E), diameter",
    "SELECT '$$' AS a, imsi AS b, '$$' AS c FROM diameter",
    'SELECT "imsi""" AS v FROM (SELECT imsi AS "imsi""" FROM diameter)',
    "SELECT x'00' AS a, imsi AS b FROM diameter",
    'SELECT x "y" FROM (SELECT imsi AS x FROM diameter)',
    # Numbers
    "SELECT 0x1F AS a, imsi AS b FROM diameter",
    "SELECT 1e+1 AS a, .5 AS b, 1. AS c, imsi AS d FROM diameter",
    "SELECT 1abc FROM (SELECT imsi AS abc FROM diameter)",
    "SELECT 1_000 AS a, imsi AS b FROM diameter",
    # Operators
    "SELECT imsi || '' AS a, +imsi AS b, - -imsi AS c, imsi + 0 AS d, imsi & -1 AS e, ~~imsi AS f, imsi << 0 AS g, "
    "imsi >> 0 AS h, imsi * 1 AS i, imsi % 999999999999999999 AS j, imsi / 1 AS k FROM diameter",
    "SELECT json_object('v', imsi) ->> 'v' AS a, json_array(imsi) -> 0 AS b FROM diameter",
    "SELECT imsi COLLATE nocase AS a, CAST(imsi AS varchar(20)) AS b, CAST(imsi AS unsigned big int) AS c "
    "FROM diameter",
    "SELECT x FROM (SELECT CAST(imsi AS TEXT) AS x FROM diameter)",
    "WITH s AS MATERIALIZED (SELECT imsi AS v FROM diameter), t AS NOT MATERIALIZED (SELECT v FROM s) "
    "SELECT v FROM t",
    "SELECT v FROM (SELECT imsi AS v FROM diameter UNION SELECT sip FROM flows) ORDER BY v",
    "SELECT v FROM (SELECT sip AS v FROM flows UNION ALL SELECT +imsi FROM diameter)",
    "SELECT \"count\"(1) AS n, \"max\"(imsi) AS v FROM diameter",
    "SELECT max (imsi) AS a, coalesce/**/(imsi, '') AS b FROM diameter",
    "SELECT group_concat(DISTINCT imsi) AS v FROM diameter",
    "SELECT max(imsi) FILTER (WHERE 1) OVER w AS v FROM diameter WINDOW w AS (ORDER BY etime)",
    # Table names
    'SELECT v FROM (SELECT imsi AS v FROM "MAIN"."DIAMETER")',
    "SELECT flows.imsi AS v FROM diameter AS flows",
    "SELECT d.imsi AS v FROM (diameter d)",
    # Inside a CTE's body, a later CTE of its WITH hides an outer one of the same name
    "WITH c AS (SELECT sip AS v FROM flows) "
    "SELECT * FROM (WITH d AS (SELECT * FROM c), c AS (SELECT imsi AS v FROM diameter) SELECT * FROM d)",
    # The name SQLite makes for the second of two columns of one name
    'SELECT (SELECT "x:1" FROM (SELECT apn AS x, imsi AS x FROM diameter)) AS v '
    'FROM (SELECT sip AS "x:1" FROM flows)',
    # In a FROM subquery or a CTE, SQLite does not look at the sources beside it, only further out
    "SELECT (SELECT s.v FROM lookup_one AS d, (SELECT d.imsi AS v) AS s LIMIT 1) AS o FROM diameter d "
    "WHERE d.imsi IS NOT NULL",
    "SELECT (WITH c AS (SELECT d.imsi AS v) SELECT c.v FROM c, lookup_one AS d LIMIT 1) AS o FROM diameter d "
    "WHERE d.imsi IS NOT NULL",
    # A column qualified with its database
    "SELECT (SELECT main.diameter.imsi FROM lookup_one AS diameter) AS o FROM diameter WHERE imsi > ''",
    # Names SQLite makes up for a subquery's columns, which an outer query also has
    'SELECT (SELECT max("1+1:1") FROM (SELECT 1+1, msisdn AS "1+1" FROM diameter)) AS o '
    'FROM (SELECT 2 AS "1+1:1")',
    'SELECT (SELECT "x:2" FROM (SELECT apn AS x, apn AS x, imsi AS "x:1" FROM diameter) a) AS o '
    'FROM (SELECT 2 AS "x:2") b',
    'SELECT (SELECT column1 FROM (SELECT imsi AS "true" FROM diameter)) AS o FROM (SELECT 2 AS column1)',
    # A subquery in a window's frame, whose value the rows counted give away
    "SELECT count(*) OVER (ORDER BY k ROWS BETWEEN (SELECT max(imsi) FROM diameter) % 7 + 0 PRECEDING "
    "AND CURRENT ROW) AS o FROM lookup_one",
    # `x IN name` reads the table of that name, though a column has the name too
    "SELECT '001010000000001' IN lookup_one AS o FROM (SELECT '1' AS lookup_one) "
    "WHERE EXISTS (SELECT 1 FROM diameter d WHERE d.imsi > '')",
])
def test_no_output_column_holding_an_identifier_is_called_unrestricted(tables, leaks, sql):
    tables.catalog.add_lookup("one", "imsi,k\n001010000000000,x")
    assert leaks(sql) in (None, [])


# Every word SQLite or the parser's lexer may take for something other than a name.
WORDS = sorted({
    word.lower() for word in [*sqlite_sql.KEYWORDS, *Postgres.Tokenizer.KEYWORDS]
    if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", word)
} | {"true", "false", "rowid", "oid", "_rowid_", "date", "e", "n", "x", "user", "sysdate", "canopy_fn"})


@pytest.mark.parametrize("query", [
    'SELECT {word} AS v FROM (SELECT imsi AS "{word}" FROM diameter)',
    'SELECT {word} v FROM (SELECT imsi AS "{word}" FROM diameter)',
    """SELECT {word} 'v' FROM (SELECT imsi AS "{word}" FROM diameter)""",
    """SELECT 'a ' || {word} AS v FROM (SELECT imsi AS "{word}" FROM diameter)""",
    'SELECT max({word}) AS v, apn FROM (SELECT imsi AS "{word}", apn FROM diameter)',
    'SELECT t.{word} AS v FROM (SELECT imsi AS "{word}" FROM diameter) t',
    "SELECT {word} AS v FROM (SELECT imsi {word} FROM diameter)",
    "SELECT * FROM (SELECT imsi {word} FROM diameter)",
    "SELECT {word} FROM (SELECT imsi AS {word} FROM diameter)",
    "WITH s({word}) AS (SELECT imsi FROM diameter) SELECT {word} AS v FROM s",
    "WITH {word} AS (SELECT imsi AS v FROM diameter) SELECT v FROM {word}",
    "SELECT {word}.imsi AS v FROM diameter AS {word}",
    "SELECT {word}(imsi) AS v FROM diameter",
])
def test_no_word_hides_an_identifier_under_a_name_of_that_word(leaks, query):
    found = {}
    for word in WORDS:
        if columns := leaks(query.format(word=word)):
            found[word] = columns
    assert found == {}


def test_every_function_of_sqlite_carries_the_identifiers_it_is_given(tables, leaks):
    with closing(sqlite3.connect(":memory:")) as con:
        functions = sorted({name for name, in con.execute("SELECT name FROM pragma_function_list")
                            if re.fullmatch(r"[a-z_][a-z0-9_]*", name)})
    found = {}
    for name in functions:
        for arguments in ("imsi", "imsi, 1", "1, imsi", "imsi, imsi, imsi"):
            sql = f"SELECT {name}({arguments}) AS v FROM diameter"
            if columns := leaks(sql):
                found[sql] = columns
    assert found == {}


def test_the_words_taken_for_keywords_without_looking_further_are_ones_sqlite_reserves():
    with closing(sqlite3.connect(":memory:")) as con:
        for word in sqlite_sql.RESERVED - {"null"}:
            with pytest.raises(sqlite3.OperationalError):
                con.execute(f'SELECT {word} FROM (SELECT 1 AS "{word}")')
