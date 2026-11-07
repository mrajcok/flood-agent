"""The Table Catalog's interface: liveness from the clock, Retention fixed at registration, the reads."""

import sqlite3
from contextlib import closing

import pytest

from flood_agent.catalog import Catalog, CatalogView, TableExpired
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


def test_read_only_view_reads_but_has_no_write_operations_and_cannot_write(world, clock):
    catalog, cid = world
    catalog.register("rt_20260101_000000", cid, 1)
    view = CatalogView(catalog.path, clock)

    assert "rt_20260101_000000" in view
    assert not hasattr(view, "register")
    with closing(view._connect()) as con, pytest.raises(sqlite3.OperationalError):
        con.execute("DELETE FROM result_tables")
    with closing(view._connect()) as con, pytest.raises(sqlite3.OperationalError):
        con.execute("INSERT INTO lookup_tables(name, created_at) VALUES ('lookup_x', 1)")
    assert "rt_20260101_000000" in catalog


def test_kind_of_any_case_covers_telemetry_result_saved_lookup_and_unknown(tmp_path, clock):
    app_db = tmp_path / "app.db"
    catalog = Catalog(app_db, clock, default_retention_ms=HOUR, telemetry_tables=["flows"])
    cid = ConversationStore(app_db, clock).create_conversation()
    catalog.register("rt_20260101_000000", cid, 1)
    catalog.register("busy_ports", cid, 1)
    with closing(sqlite3.connect(app_db)) as con, con:
        con.execute("INSERT INTO lookup_tables(name, created_at) VALUES ('lookup_ips', 1)")
    view = CatalogView(app_db, clock, ["flows"])

    kinds = {n: view.kind_of(n) for n in ("FLOWS", "rt_20260101_000000", "Busy_Ports", "lookup_IPS", "nope")}

    assert kinds == {"FLOWS": "telemetry", "rt_20260101_000000": "result", "Busy_Ports": "saved",
                     "lookup_IPS": "lookup", "nope": None}
    assert view.readable() == {"rt_20260101_000000", "busy_ports", "lookup_ips"}
    clock.now += HOUR
    assert view.kind_of("busy_ports") is None
    assert view.readable() == {"lookup_ips"}


# --- listing, kinds, deletes and ownership (#82) -------------------------------------------------------


def _physical(path, *names):
    import sqlite3

    con = sqlite3.connect(path)
    for n in names:
        con.execute(f'CREATE TABLE "{n}" (x)')
    con.commit()
    con.close()


def _exists(path, name) -> bool:
    import sqlite3

    con = sqlite3.connect(path)
    try:
        return con.execute("SELECT 1 FROM sqlite_master WHERE name=?", (name,)).fetchone() is not None
    finally:
        con.close()


@pytest.fixture
def full(tmp_path, clock):
    """(catalog, conversation id, results db path): a Catalog that knows a Telemetry table `flows`."""
    app_db, results_db = tmp_path / "app.db", tmp_path / "results.db"
    catalog = Catalog(app_db, clock, default_retention_ms=HOUR, results_db=results_db, telemetry_tables=["flows"])
    return catalog, ConversationStore(app_db, clock).create_conversation(), results_db


def test_kind_of_a_name_in_any_case(full):
    catalog, cid, results_db = full
    _physical(results_db, "rt_20260101_000000", "kept")
    catalog.register("rt_20260101_000000", cid, 1)
    catalog.register("kept", cid, 1)
    catalog.add_lookup("hosts", "ip\n1.1.1.1")
    assert [catalog.kind_of(n) for n in ("FLOWS", "RT_20260101_000000", "Kept", "lookup_Hosts", "nope")] == [
        "telemetry", "result", "saved", "lookup", None]


def test_listing_gives_every_kind_with_retention_and_time_left(full, clock):
    catalog, cid, results_db = full
    _physical(results_db, "rt_20260101_000000", "kept")
    catalog.register("rt_20260101_000000", cid, 1)
    catalog.register("kept", cid, 1)
    catalog.add_lookup("hosts", "ip\n1.1.1.1")
    clock.now += HOUR // 4
    assert catalog.listing() == {
        "telemetry": ["flows"], "lookup": ["lookup_hosts"], "saved": ["kept"], "result": ["rt_20260101_000000"],
        "retention_days": {"kept": HOUR / DAY, "rt_20260101_000000": HOUR / DAY},
        "expires_in_ms": {"kept": HOUR * 3 // 4, "rt_20260101_000000": HOUR * 3 // 4}}


def test_delete_drops_the_table_but_keeps_it_registered_as_expired(full):
    catalog, cid, results_db = full
    _physical(results_db, "kept")
    catalog.register("kept", cid, 1)
    catalog.delete("kept")
    assert not _exists(results_db, "kept")
    with pytest.raises(TableExpired):
        catalog["kept"]
    with pytest.raises(KeyError):
        catalog.delete("kept")  # no longer a live table


def test_delete_of_a_lookup_table_unregisters_it_and_a_telemetry_table_is_refused(full):
    catalog, _, results_db = full
    catalog.add_lookup("hosts", "ip\n1.1.1.1")
    catalog.delete("LOOKUP_hosts")
    assert catalog.kind_of("lookup_hosts") is None and not _exists(results_db, "lookup_hosts")
    with pytest.raises(ValueError, match="Telemetry"):
        catalog.delete("flows")
    with pytest.raises(KeyError):
        catalog.delete("nope")


def test_owned_tables_follow_the_recorded_turn_split_into_result_and_saved(full):
    catalog, cid, _ = full
    catalog.register("rt_20260101_000000", cid, 1)
    catalog.register("kept", cid, 2)  # a Saved Table (renamed since) first used by turn 2
    catalog.register("rt_20260101_000001", cid, 2)
    assert [(len(o.result), len(o.saved)) for o in (catalog.owned(cid), catalog.owned(cid, 1), catalog.owned(cid, 2))] == [
        (2, 1), (1, 0), (1, 1)]


def test_drop_owned_drops_only_rt_tables_of_that_conversation_or_turn(full):
    catalog, cid, results_db = full
    other = ConversationStore(catalog.path).create_conversation()
    _physical(results_db, "rt_a", "rt_b", "kept", "rt_c")
    catalog.register("rt_a", cid, 1)
    catalog.register("rt_b", cid, 2)
    catalog.register("kept", cid, 2)
    catalog.register("rt_c", other, 3)
    assert catalog.drop_owned(cid, 1) == ["rt_a"]
    assert not _exists(results_db, "rt_a") and _exists(results_db, "rt_b")
    assert catalog.drop_owned(cid) == ["rt_b"]
    assert _exists(results_db, "kept") and _exists(results_db, "rt_c")
    assert "kept" in catalog and "rt_c" in catalog and "rt_b" not in catalog
