"""The query runner (spec #194): what a caller gets from it. Query safety, limits and the count form are tested in
their own files."""

import sqlite3

import pytest

from flood_agent import queries


def _locked_app_database() -> set[str]:
    raise sqlite3.OperationalError("the app database is locked")


def test_a_table_made_readable_after_the_runner_was_built_can_be_read(query_runner):
    readable: set[str] = set()
    runner = query_runner(readable)
    first = runner.run("SELECT dport FROM flows", max_rows=10, title="ports")["result_table"]
    sql = f"SELECT dport FROM {first}"

    refused = runner.run(sql, max_rows=10)
    assert refused["success"] is False and refused["error"] == queries.ACCESS_DENIED

    readable.add(first)
    assert runner.run(sql, max_rows=10)["row_count"] == 3
    assert runner.count(f"SELECT COUNT(*) FROM {first}") == {"success": True, "count": 3}


@pytest.mark.parametrize("operation", [
    lambda runner: runner.run("SELECT 1", max_rows=10),
    lambda runner: runner.count("SELECT COUNT(*) FROM flows"),
    lambda runner: runner.explain("SELECT * FROM flows"),
    lambda runner: runner.describe("t"),
    lambda runner: runner.table_columns("t", telemetry=False),
    lambda runner: runner.row_count("t"),
    lambda runner: runner.result_columns("t"),
    lambda runner: runner.fetch_rows("t"),
    lambda runner: runner.all_rows("t"),
    lambda runner: runner.chart_rows("t", 10),
    lambda runner: runner.drop("t"),
    lambda runner: runner.swap("temp", "t"),
    lambda runner: runner.rename("t", "other"),
])
def test_a_database_error_is_the_runners_own_error(query_runner, tmp_path, operation):
    (tmp_path / "results.db").write_text("this is not a database " * 100)
    with pytest.raises(queries.QueryFailed):
        operation(query_runner())


@pytest.mark.parametrize("operation", [
    lambda runner: runner.run("SELECT 1", max_rows=10),
    lambda runner: runner.count("SELECT COUNT(*) FROM flows"),
])
def test_run_and_count_do_not_turn_a_failure_to_read_the_readable_set_into_a_query_failure(query_runner, operation):
    with pytest.raises(sqlite3.OperationalError):
        operation(query_runner(_locked_app_database))


def test_explain_reports_a_failure_to_read_the_readable_set_as_a_query_failure(query_runner):
    with pytest.raises(queries.QueryFailed):
        query_runner(_locked_app_database).explain("SELECT * FROM flows")


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
