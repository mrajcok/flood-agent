"""The Table Catalog's Lookup Table operations: add (with Overwrite), replace, and rows as text."""

import sqlite3

import pytest

from flood_agent.catalog import Catalog
from flood_agent.lookup_tables import InvalidLookupTable

DAY = 86_400_000


class Clock:
    def __init__(self):
        self.now = 1_000_000_000_000

    def __call__(self) -> int:
        return self.now


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def lookups(tmp_path, clock):
    """(catalog, results database path): a Catalog that can write Lookup Tables."""
    app_db, results_db = tmp_path / "app.db", tmp_path / "results.db"
    return Catalog(app_db, clock, results_db=results_db), results_db


def stored(results_db, name):
    with sqlite3.connect(results_db) as con:
        return [list(r) for r in con.execute(f'SELECT * FROM "{name}" ORDER BY rowid')]


def test_add_lowercases_the_name_and_stores_the_rows(lookups):
    catalog, results_db = lookups
    assert catalog.add_lookup("WatchList", "ip,owner\n10.0.0.1,ops\n10.0.0.2,") == {
        "name": "lookup_watchlist", "row_count": 2, "columns": ["ip", "owner"]}
    assert stored(results_db, "lookup_watchlist") == [["10.0.0.1", "ops"], ["10.0.0.2", None]]


def test_lookup_tables_never_expire(lookups, clock):
    catalog, _ = lookups
    catalog.add_lookup("hosts", "ip\n10.0.0.1")
    clock.now += 4000 * DAY
    assert catalog.lookup_text("lookup_hosts", "comma") == {"name": "lookup_hosts", "text": "ip\n10.0.0.1\n"}


def test_add_refuses_a_name_already_used_unless_overwriting_a_lookup_table(lookups):
    catalog, results_db = lookups
    with sqlite3.connect(results_db) as con:
        con.execute('CREATE TABLE "lookup_other" (x TEXT)')
    with pytest.raises(InvalidLookupTable, match="not a Lookup Table"):
        catalog.add_lookup("other", "ip\n1", overwrite=True)  # Overwrite never takes over another table
    catalog.add_lookup("hosts", "ip\n10.0.0.1")
    with pytest.raises(InvalidLookupTable, match="Overwrite"):
        catalog.add_lookup("hosts", "ip\n10.0.0.2")
    assert stored(results_db, "lookup_hosts") == [["10.0.0.1"]]
    result = catalog.add_lookup("hosts", "ip,note\n10.0.0.2,x", overwrite=True)
    assert result["columns"] == ["ip", "note"]
    assert stored(results_db, "lookup_hosts") == [["10.0.0.2", "x"]]


def test_add_rejects_a_bad_paste_and_registers_nothing(lookups):
    catalog, _ = lookups
    with pytest.raises(InvalidLookupTable):
        catalog.add_lookup("hosts", "ip")
    with pytest.raises(KeyError):
        catalog.lookup_text("lookup_hosts", "comma")


def test_replace_swaps_the_rows_and_keeps_the_name(lookups):
    catalog, results_db = lookups
    catalog.add_lookup("hosts", "ip\n10.0.0.1")
    assert catalog.replace_lookup("LOOKUP_HOSTS", "ip|owner\n10.0.0.5|ops") == {
        "name": "lookup_hosts", "row_count": 1, "columns": ["ip", "owner"]}
    assert stored(results_db, "lookup_hosts") == [["10.0.0.5", "ops"]]
    with pytest.raises(InvalidLookupTable):
        catalog.replace_lookup("lookup_hosts", "ip")
    assert stored(results_db, "lookup_hosts") == [["10.0.0.5", "ops"]]
    with pytest.raises(KeyError):
        catalog.replace_lookup("lookup_nope", "ip\n1")


def test_text_in_each_separator(lookups):
    catalog, _ = lookups
    catalog.add_lookup("hosts", 'ip,desc\n10.0.0.1,"edge, east"\n10.0.0.2,\n10.0.0.3,a|b')
    assert catalog.lookup_text("Lookup_Hosts", "comma") == {
        "name": "lookup_hosts", "text": 'ip,desc\n10.0.0.1,"edge, east"\n10.0.0.2,\n10.0.0.3,a|b\n'}
    assert catalog.lookup_text("lookup_hosts", "pipe")["text"] == (
        'ip|desc\n10.0.0.1|edge, east\n10.0.0.2|\n10.0.0.3|"a|b"\n')
    with pytest.raises(InvalidLookupTable, match="comma, pipe"):
        catalog.lookup_text("lookup_hosts", "tab")
    with pytest.raises(KeyError):
        catalog.lookup_text("flows", "comma")
