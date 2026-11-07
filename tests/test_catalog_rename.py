"""The Table Catalog's rename and Retention rules: lower-case names, both files, the store's hook."""

import sqlite3
from contextlib import closing

import pytest

from flood_agent.catalog import Catalog
from flood_agent.store import ConversationStore

HOUR = 3_600_000
DAY = 24 * HOUR
RT = "rt_20260101_000000"
RT2 = "rt_20260101_000001"


class Clock:
    def __init__(self):
        self.now = 1_000_000_000_000

    def __call__(self) -> int:
        return self.now


def physical(results_db) -> set[str]:
    with closing(sqlite3.connect(results_db)) as con:
        return {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def make(tmp_path, on_rename=None, telemetry=("flows",)):
    """(catalog, conversation id, results db path) with one Result Table, registered and physical, 1-day default."""
    app_db, results_db = tmp_path / "app.db", tmp_path / "results.db"
    clock = Clock()
    catalog = Catalog(app_db, clock, default_retention_ms=DAY, telemetry_tables=telemetry, results_db=results_db,
                      on_rename=on_rename)
    cid = ConversationStore(app_db, clock).create_conversation()
    with closing(sqlite3.connect(results_db)) as con, con:
        con.execute(f'CREATE TABLE "{RT}" (n INTEGER)')
    catalog.register(RT, cid, 1)
    return catalog, cid, results_db


def add_second(catalog, cid, results_db):
    with closing(sqlite3.connect(results_db)) as con, con:
        con.execute(f'CREATE TABLE "{RT2}" (n INTEGER)')
    catalog.register(RT2, cid, 1)


def test_rename_lowercases_and_every_lookup_ignores_case(tmp_path):
    catalog, _, results_db = make(tmp_path)

    table = catalog.update(RT, "MyTable")

    assert table.name == "mytable" and table.kind == "saved"
    assert catalog["MyTable"].name == "mytable" and "MYTABLE" in catalog
    assert catalog.names() == ["mytable"] and catalog.live_among(["MyTable"]) == {"mytable"}
    assert "mytable" in physical(results_db) and RT not in physical(results_db)
    assert RT not in catalog


def test_rename_without_retention_raises_a_short_one_to_30_days(tmp_path):
    catalog, _, _ = make(tmp_path)
    assert catalog.update(RT, "kept").retention_days == 30


def test_rename_keeps_a_longer_retention(tmp_path):
    catalog, _, _ = make(tmp_path)
    catalog.update(RT, retention_days=90)
    assert catalog.update(RT, "kept").retention_days == 90


def test_retention_given_with_the_rename_wins(tmp_path):
    catalog, _, _ = make(tmp_path)
    assert catalog.update(RT, "kept", retention_days=2).retention_days == 2


def test_retention_alone_changes_only_retention(tmp_path):
    catalog, _, _ = make(tmp_path)
    table = catalog.update(RT, retention_days=5)
    assert (table.name, table.retention_days) == (RT, 5)


def test_renaming_a_saved_table_leaves_its_retention(tmp_path):
    catalog, _, _ = make(tmp_path)
    catalog.update(RT, "kept", retention_days=3)
    assert catalog.update("kept", "kept_too").retention_days == 3


@pytest.mark.parametrize("days", [0, -1, 3650.5, float("nan")])
def test_a_retention_outside_the_range_is_refused_and_nothing_changes(tmp_path, days):
    catalog, _, results_db = make(tmp_path)
    with pytest.raises(ValueError):
        catalog.update(RT, "kept", retention_days=days)
    assert catalog[RT].retention_days == 1 and RT in physical(results_db)


def test_the_cap_itself_is_allowed(tmp_path):
    catalog, _, _ = make(tmp_path)
    assert catalog.update(RT, retention_days=3650).retention_days == 3650


@pytest.mark.parametrize("new", ["rt_mine", "Lookup_x", "1bad", "has space", "flows", "FLOWS"])
def test_refused_names_change_nothing(tmp_path, new):
    catalog, _, results_db = make(tmp_path)
    with pytest.raises(ValueError):
        catalog.update(RT, new)
    assert catalog[RT].name == RT and RT in physical(results_db)


def test_a_taken_name_is_refused(tmp_path):
    catalog, cid, results_db = make(tmp_path)
    add_second(catalog, cid, results_db)
    catalog.update(RT2, "mine")
    with pytest.raises(ValueError, match="already"):
        catalog.update(RT, "MINE")
    assert catalog[RT].name == RT


def test_a_name_held_by_a_dropped_table_is_reported_as_taken(tmp_path):
    catalog, cid, results_db = make(tmp_path)
    add_second(catalog, cid, results_db)
    catalog.update(RT2, "gone")
    with closing(sqlite3.connect(catalog.path)) as con, con:  # dropped: still registered, no physical table
        con.execute("UPDATE result_tables SET expired=1 WHERE name='gone'")
    with closing(sqlite3.connect(results_db)) as con, con:
        con.execute('DROP TABLE "gone"')
    with pytest.raises(ValueError, match="already"):
        catalog.update(RT, "gone")
    assert catalog[RT].name == RT and RT in physical(results_db)


def test_the_hook_runs_in_the_registry_transaction_and_sees_old_and_new(tmp_path):
    seen = []

    def hook(con, old, new):
        seen.append((old, new, con.execute("SELECT name FROM result_tables").fetchall()[0][0]))

    catalog, _, _ = make(tmp_path, on_rename=hook)
    catalog.update(RT, "Kept")
    assert seen == [(RT, "kept", "kept")]


def test_when_the_registry_update_fails_the_physical_rename_is_undone(tmp_path):
    def hook(con, old, new):
        raise RuntimeError("boom")

    catalog, _, results_db = make(tmp_path, on_rename=hook)
    with pytest.raises(RuntimeError):
        catalog.update(RT, "kept")
    assert catalog[RT].name == RT
    assert RT in physical(results_db) and "kept" not in physical(results_db)


def test_register_lowercases_the_name(tmp_path):
    catalog, cid, _ = make(tmp_path)
    catalog.register("RT_20260101_000002", cid, 1)
    assert catalog["rt_20260101_000002"].name == "rt_20260101_000002"


def test_the_retention_policy_is_exposed(tmp_path):
    catalog, _, _ = make(tmp_path)
    assert (catalog.policy.result_days, catalog.policy.saved_days, catalog.policy.max_days) == (1, 30, 3650)
