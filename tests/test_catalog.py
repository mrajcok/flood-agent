"""The Table Catalog's interface: liveness from the clock, Retention fixed at registration, the reads."""

import sqlite3
from datetime import datetime

import pytest

from flood_agent.catalog import Catalog, TableExpired
from flood_agent.store import ConversationStore

HOUR = 3_600_000
DAY = 24 * HOUR


class Clock:
    def __init__(self, now: int = 1_000_000_000_000):
        self.now = now

    def __call__(self) -> int:
        return self.now


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def world(tmp_path, clock):
    """(catalog, conversation id): a Catalog with a 1-hour default Retention over a fresh app database."""
    app_db = tmp_path / "app.db"
    catalog = Catalog(app_db, clock, default_retention_ms=HOUR)
    conversation = ConversationStore(app_db, clock).create_conversation()
    return catalog, conversation


def test_registered_table_is_indexable_with_its_record(world):
    catalog, cid = world
    columns = [{"name": "n", "type": "INTEGER"}]
    catalog.register("rt_20260101_000000", cid, 7, "Top ports", columns, "show ports", 500)

    table = catalog["rt_20260101_000000"]

    assert (table.name, table.kind, table.conversation_id, table.turn_id) == ("rt_20260101_000000", "result", cid, 7)
    assert (table.title, table.columns, table.request, table.truncated_to) == ("Top ports", columns, "show ports", 500)
    assert (table.retention_ms, table.expires_in_ms) == (HOUR, HOUR)
    assert "rt_20260101_000000" in catalog


def test_a_table_not_named_rt_is_a_saved_table(world):
    catalog, cid = world
    catalog.register("busy_ports", cid, 1)
    assert catalog["busy_ports"].kind == "saved"


def test_unknown_name_is_not_found_and_not_a_member(world):
    catalog, _ = world
    with pytest.raises(KeyError):
        catalog["nope"]
    assert "nope" not in catalog


def test_table_is_live_until_the_exact_expiry_instant_with_no_sweep(world, clock):
    catalog, cid = world
    catalog.register("rt_a", cid, 1)
    start = clock.now

    clock.now = start + HOUR - 1
    assert "rt_a" in catalog and catalog["rt_a"].expires_in_ms == 1
    assert catalog.live_among(["rt_a"]) == {"rt_a"}
    assert [t.name for t in catalog.tables_of(cid)] == ["rt_a"]

    clock.now = start + HOUR
    assert "rt_a" not in catalog
    with pytest.raises(TableExpired):
        catalog["rt_a"]
    assert catalog.live_among(["rt_a"]) == set()
    assert catalog.tables_of(cid) == []
    assert catalog.names() == []


def test_table_expired_is_not_a_key_error():
    assert not issubclass(TableExpired, KeyError)


def test_retention_is_fixed_at_registration(tmp_path, clock):
    app_db = tmp_path / "app.db"
    cid = ConversationStore(app_db, clock).create_conversation()
    Catalog(app_db, clock, default_retention_ms=HOUR).register("rt_a", cid, 1)

    changed = Catalog(app_db, clock, default_retention_ms=DAY)  # the configured default changed later
    changed.register("rt_b", cid, 1)

    assert changed["rt_a"].retention_ms == HOUR
    assert changed["rt_b"].retention_ms == DAY


def test_live_among_returns_only_live_names(world, clock):
    catalog, cid = world
    catalog.register("rt_old", cid, 1)
    clock.now += HOUR
    catalog.register("rt_new", cid, 2)

    assert catalog.live_among(["rt_old", "rt_new", "unknown"]) == {"rt_new"}


def test_a_conversations_tables_are_its_own_live_ones_in_creation_order(world, clock, tmp_path):
    catalog, cid = world
    other = ConversationStore(tmp_path / "app.db", clock).create_conversation()
    catalog.register("rt_b", cid, 1)
    clock.now += 1
    catalog.register("rt_a", cid, 2)
    catalog.register("rt_x", other, 3)

    assert [t.name for t in catalog.tables_of(cid)] == ["rt_b", "rt_a"]
    assert [t.turn_id for t in catalog.tables_of(cid)] == [1, 2]
    assert catalog.names() == ["rt_a", "rt_b", "rt_x"]


# --- the sweep ---

def _results_db(tmp_path, *names):
    path = tmp_path / "results.db"
    con = sqlite3.connect(path)
    for n in names:
        con.execute(f'CREATE TABLE "{n}" (x INTEGER)')
    con.commit()
    con.close()
    return path


def _physical(path):
    con = sqlite3.connect(path)
    try:
        return {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    finally:
        con.close()


def _swept_world(tmp_path, clock, *physical):
    results = _results_db(tmp_path, *physical)
    app_db = tmp_path / "app.db"
    catalog = Catalog(app_db, clock, default_retention_ms=HOUR, results_db=results)
    return catalog, ConversationStore(app_db, clock).create_conversation(), results


def test_sweep_drops_an_expired_table_and_keeps_it_registered_as_dropped(tmp_path, clock):
    catalog, cid, results = _swept_world(tmp_path, clock, "rt_a", "rt_b")
    catalog.register("rt_a", cid, 1)
    clock.now += HOUR - 1
    catalog.register("rt_b", cid, 1)
    clock.now += 1  # rt_a is due, rt_b is not

    catalog.sweep()

    assert _physical(results) == {"rt_b"}
    with pytest.raises(TableExpired):
        catalog["rt_a"]  # still registered, now dropped
    assert "rt_b" in catalog


def test_sweep_drops_only_old_unregistered_rt_tables(tmp_path, clock):
    clock.now = int(datetime(2026, 5, 1, 12, 0, 0).timestamp() * 1000)  # local time, like the name generator
    catalog, cid, results = _swept_world(
        tmp_path, clock, "rt_20260501_105959", "rt_20260501_110001", "rt_20260501_105900_2", "lookup_x", "mine", "rt_live")
    catalog.register("rt_live", cid, 1)
    catalog.register("mine", cid, 1)

    catalog.sweep()

    assert _physical(results) == {"rt_20260501_110001", "lookup_x", "mine", "rt_live"}


def test_sweep_without_a_results_database_is_harmless(world):
    catalog, cid = world
    catalog.register("rt_a", cid, 1)
    catalog.sweep()
    assert "rt_a" in catalog
