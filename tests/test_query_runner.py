"""The query runner (spec #194): what a caller gets from it. Query safety, limits and the count form are tested in
their own files."""

import pytest
from conftest import ScriptedLLM, register_table

from flood_agent import queries


def test_a_table_made_readable_after_the_runner_was_built_can_be_read(query_runner):
    readable: set[str] = set()
    runner = query_runner(readable)
    first = runner.run("SELECT dport FROM flows", max_rows=10, title="ports")["result_table"]
    sql = f"SELECT dport FROM {first}"

    refused = runner.run(sql, max_rows=10)
    assert refused["success"] is False and refused["error"] == queries.ACCESS_DENIED

    readable.add(first)
    assert runner.run(sql, max_rows=10)["row_count"] == 3


@pytest.mark.parametrize("operation", [
    lambda runner: runner.explain("SELECT * FROM nosuch_table"),
    lambda runner: runner.swap("no_temp", "no_table"),
    lambda runner: runner.rename("no_table", "other"),
])
def test_a_database_error_is_the_runners_own_error(query_runner, operation):
    with pytest.raises(queries.QueryFailed):
        operation(query_runner())


@pytest.mark.parametrize("operation", [
    lambda runner: runner.run("SELECT 1", max_rows=10),
    lambda runner: runner.count("SELECT COUNT(*) FROM flows"),
    lambda runner: runner.describe("t"),
    lambda runner: runner.table_columns("t", telemetry=False),
    lambda runner: runner.row_count("t"),
    lambda runner: runner.result_columns("t"),
    lambda runner: runner.fetch_rows("t"),
    lambda runner: runner.all_rows("t"),
    lambda runner: runner.chart_rows("t", 10),
    lambda runner: runner.drop("t"),
])
def test_an_unreadable_results_database_is_the_runners_own_error(query_runner, tmp_path, operation):
    (tmp_path / "results.db").write_text("this is not a database " * 100)
    with pytest.raises(queries.QueryFailed):
        operation(query_runner())


def test_the_error_holds_no_value_from_the_query(query_runner):
    with pytest.raises(queries.QueryFailed) as raised:
        query_runner().explain("SELECT * FROM flows WHERE sip = 172.30.41.7")
    assert "172" not in str(raised.value) and "41" not in str(raised.value)


def test_drop_swap_and_rename_change_the_tables_in_the_results_database(query_runner):
    runner = query_runner()
    old = runner.run("SELECT dport FROM flows", max_rows=10)["result_table"]
    new = runner.run("SELECT sip FROM flows", max_rows=2)["result_table"]

    runner.swap(new, old)
    assert (runner.result_columns(old), runner.row_count(old), runner.result_columns(new)) == (["sip"], 2, None)

    runner.rename(old, "victims")
    assert (runner.row_count(old), runner.row_count("victims")) == (None, 2)
    taken = runner.run("SELECT 1 AS one", max_rows=10)["result_table"]
    with pytest.raises(ValueError):
        runner.rename(taken, "victims")

    runner.drop("victims")
    runner.drop("victims")  # dropping a table that is gone is fine
    assert runner.describe("victims") is None


def test_a_tables_rows_and_columns_can_be_read(query_runner):
    runner = query_runner()
    name = runner.run("SELECT dport FROM flows", max_rows=10)["result_table"]

    page = runner.fetch_rows(name, page=2, page_size=1, sort="dport", descending=True)
    assert (page["rows"], page["total"]) == ([[8443]], 3)
    columns, rows = runner.all_rows(name)
    assert (columns, list(rows)) == (["dport"], [(8443,), (8443,), (9053,)])
    assert runner.chart_rows(name, 2) == [{"dport": 8443}, {"dport": 8443}]
    assert runner.describe(name) == {"columns": [{"name": "dport", "type": "INT"}], "row_count": 3}
    assert runner.table_columns("flows", telemetry=True)[0] == {"name": "sip", "type": "TEXT", "insert": "sip"}


def test_count_and_explain_read_only_the_readable_tables(query_runner):
    readable: set[str] = set()
    runner = query_runner(readable)
    name = runner.run("SELECT dport FROM flows", max_rows=10)["result_table"]
    sql = f"SELECT COUNT(dport) FROM {name}"

    assert runner.count(sql)["error"] == queries.ACCESS_DENIED
    with pytest.raises(queries.QueryFailed):
        runner.explain(sql)

    readable.add(name)
    assert runner.count(sql) == {"success": True, "count": 3}
    assert runner.explain(sql)


def test_the_agents_runner_reads_what_its_table_catalog_makes_readable(build_agent):
    agent = build_agent(ScriptedLLM())
    name = agent.queries.run("SELECT dport FROM flows", max_rows=10)["result_table"]
    sql = f"SELECT dport FROM {name}"
    assert agent.queries.run(sql, max_rows=10)["success"] is False

    register_table(agent.tables, name)
    assert agent.queries.run(sql, max_rows=10)["row_count"] == 3
