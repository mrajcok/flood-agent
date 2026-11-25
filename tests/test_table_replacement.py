"""Table replacement: `replace` puts a query's rows under a live table's name, all or nothing. Tested at its
interface against a real results database and Table Catalog (no Agent turn, no web)."""

import sqlite3
import threading
from contextlib import closing
from dataclasses import replace

import pytest
from conftest import DAY, Clock, ScriptedLLM

from flood_agent.catalog import CellRun, TableExpired
from flood_agent.queries import QueryFailed
from flood_agent.table_replacement import RefreshInProgress, ReplacementFailed, Rerun, TableReplacement

HOUR = 3_600_000
FIRST = "SELECT dport FROM flows ORDER BY dport"
WIDER = "SELECT sip, dport FROM flows ORDER BY dport"
OWNER = CellRun("cell-1", "run-1")


@pytest.fixture
def setup(build_agent, query_runner):
    """An Agent (not started) with a clock, and a table of FIRST's rows capped at 2."""
    clock = Clock()
    agent = build_agent(ScriptedLLM(), clock=clock, result_ttl_ms=DAY)
    agent.replacement = TableReplacement(agent.tables, query_runner(agent.tables.readable))
    return agent, clock, make_table(agent, FIRST, 2)


def make_table(agent, sql, max_rows):
    result = agent.replacement.runner.run(sql, max_rows, "First")
    agent.tables.register_for(OWNER, result["result_table"], "First", result["columns"], "", result.get("truncated_to"),
                              sql, max_rows)
    return result["result_table"]


def described(table):
    """What the catalog says about a table, apart from the time left (which moves with the clock)."""
    return replace(table, expires_in_ms=0)


def stored_rows(agent, name):
    with closing(sqlite3.connect(agent.config.results_db)) as con:
        return con.execute(f'SELECT * FROM "{name}"').fetchall()


def tables_in_results_db(agent):
    with closing(sqlite3.connect(agent.config.results_db)) as con:
        return sorted(n for (n,) in con.execute("SELECT name FROM sqlite_master WHERE type='table'"))


def test_replace_puts_new_rows_under_the_same_name_and_describes_them(setup):
    agent, clock, name = setup
    old = agent.tables[name]
    assert len(stored_rows(agent, name)) == 2 and old.truncated_to == 2
    clock.ms += HOUR

    done = agent.replacement.replace(name, WIDER, 5)
    new = done.table

    assert done.row_count == 3 and new.name == name and [c["name"] for c in new.columns] == ["sip", "dport"]
    assert len(stored_rows(agent, name)) == 3 and len(stored_rows(agent, name)[0]) == 2
    assert new.truncated_to is None  # the old table was cut to 2; these rows are all there are
    assert new.as_of_ms == clock.ms and new.expires_in_ms == old.retention_ms  # Retention restarted, same length
    assert (new.sql, new.max_rows, new.owner, new.title) == (old.sql, old.max_rows, old.owner, old.title)
    assert tables_in_results_db(agent) == [name]  # no temporary table is left


def test_truncation_is_reported_only_while_the_row_cap_cuts_the_rows(setup):
    agent, _, name = setup
    assert agent.replacement.replace(name, FIRST, 2).table.truncated_to == 2
    assert agent.replacement.replace(name, FIRST, 1000).table.truncated_to is None


def test_a_failed_query_changes_nothing(setup):
    agent, clock, name = setup
    before, rows = described(agent.tables[name]), stored_rows(agent, name)
    clock.ms += HOUR

    with pytest.raises(ReplacementFailed) as e:
        agent.replacement.replace(name, "SELECT nosuchcolumn FROM flows", 5)

    assert not e.value.stopped and e.value.error and e.value.raw_error
    assert described(agent.tables[name]) == before and stored_rows(agent, name) == rows
    assert tables_in_results_db(agent) == [name]


def test_a_stopped_query_changes_nothing(setup):
    agent, _, name = setup
    before, rows = described(agent.tables[name]), stored_rows(agent, name)
    stop = threading.Event()
    stop.set()
    slow = ("WITH RECURSIVE c(x) AS (SELECT 1 UNION ALL SELECT x + 1 FROM c WHERE x < 400000000) "
            "SELECT SUM(x) AS s FROM c")

    with pytest.raises(ReplacementFailed) as e:
        agent.replacement.replace(name, slow, 5, stop=stop)

    assert e.value.stopped
    assert described(agent.tables[name]) == before and stored_rows(agent, name) == rows
    assert tables_in_results_db(agent) == [name]


def test_a_table_that_is_not_live_is_refused_and_nothing_changes(setup):
    agent, clock, name = setup
    clock.ms += 2 * DAY
    agent.tables.sweep()  # Retention is over: the table's rows are dropped

    with pytest.raises(TableExpired):
        agent.replacement.replace(name, FIRST, 5)

    assert tables_in_results_db(agent) == []
    with pytest.raises(TableExpired):  # it stays expired: the catalog was not written
        agent.tables[name]
    assert agent.tables.tables_of_run(OWNER.cell_id, OWNER.run_id) == []


def test_a_sweep_dropping_the_table_mid_replacement_gives_the_plain_expired_answer(setup, monkeypatch):
    agent, clock, name = setup
    real = agent.replacement.runner.run

    def sweep_during(*args, **kwargs):
        result = real(*args, **kwargs)
        clock.ms += 2 * DAY
        agent.tables.sweep()
        return result

    monkeypatch.setattr(agent.replacement.runner, "run", sweep_during)
    with pytest.raises(TableExpired):
        agent.replacement.replace(name, FIRST, 5)
    assert tables_in_results_db(agent) == []  # no temporary table is left behind


def test_a_failed_swap_restores_the_catalog_and_keeps_the_old_rows(setup, monkeypatch):
    agent, clock, name = setup
    before, rows = described(agent.tables[name]), stored_rows(agent, name)
    clock.ms += HOUR

    def broken(*args, **kwargs):
        raise QueryFailed("disk I/O error")

    monkeypatch.setattr(agent.replacement.runner, "swap", broken)
    with pytest.raises(QueryFailed):
        agent.replacement.replace(name, WIDER, 5, rerun=Rerun(CellRun("cell-2", "run-2"), "Second"))

    assert described(agent.tables[name]) == before  # columns, truncation, as-of time, owner, title, SQL and row cap
    assert stored_rows(agent, name) == rows and tables_in_results_db(agent) == [name]


def test_a_rerun_re_owns_the_table_in_the_same_replacement(setup):
    agent, _, name = setup
    new_owner = CellRun("cell-2", "run-2")

    new = agent.replacement.replace(name, WIDER, 3, rerun=Rerun(new_owner, "Second")).table

    assert (new.owner, new.title, new.sql, new.max_rows) == (new_owner, "Second", WIDER, 3)
    assert agent.tables.tables_of_run("cell-2", "run-2") == [new]
    assert agent.tables.tables_of_run("cell-1", "run-1") == []


def test_a_second_replacement_of_the_same_table_is_refused_while_one_runs(setup, monkeypatch):
    agent, _, name = setup
    other = make_table(agent, FIRST, 3)
    started, release, real = threading.Event(), threading.Event(), agent.replacement.runner.run

    def slow(*args, **kwargs):
        started.set()
        assert release.wait(5)
        return real(*args, **kwargs)

    monkeypatch.setattr(agent.replacement.runner, "run", slow)
    done = []
    t = threading.Thread(target=lambda: done.append(agent.replacement.replace(name, FIRST, 2)))
    t.start()
    assert started.wait(5)
    try:
        with pytest.raises(RefreshInProgress, match="already being replaced"):
            agent.replacement.replace(name, FIRST, 2)
        with pytest.raises(RefreshInProgress):  # a SQL Cell re-run of the same table is refused too
            agent.replacement.replace(name, FIRST, 2, rerun=Rerun(CellRun("c", "r"), "T"))
        monkeypatch.setattr(agent.replacement.runner, "run", real)
        agent.replacement.replace(other, FIRST, 3)  # a different table is not blocked
    finally:
        release.set()
        t.join(5)
    assert len(done) == 1
    monkeypatch.setattr(agent.replacement.runner, "run", real)
    agent.replacement.replace(name, FIRST, 2)  # the guard was released
