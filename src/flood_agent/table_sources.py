"""Table sources: the Telemetry Database tables a query's data came from, which is what decides whether a table made
from it is restricted (ADR 0008).

They are established from SQLite itself, never by parsing SQL: the tables and indexes the compiled program opens,
to which the authorizer's reports only add (it says nothing of the column a USING or NATURAL join matches on). What is
recognised is an allow-list, and anything else makes the sources unknown.
"""

import json
import logging
import sqlite3
from typing import Callable

log = logging.getLogger("uvicorn.error")

Sources = frozenset[str] | None
"""The Telemetry Database tables (names folded by `fold`) a table's data came from. None: not known. The empty set:
from none of them."""

Recorded = Callable[[str], Sources]
"""The record of a table in the results database, by name: none for a Lookup Table, None if it is not known."""

Authorizer = Callable[[int, str | None, str | None, str | None, str | None], int]

_TELEMETRY, _OWN = "main", "results"  # the SQLite names of the Telemetry Database and the User's results database
_DATABASE_OPENS = frozenset({"OpenRead", "ReopenIdx"})  # a cursor on the table or index at root page p2 of database p3
# Cursors on what the program itself makes, from rows it read through other cursors.
_TRANSIENT_OPENS = frozenset({"OpenEphemeral", "OpenAutoindex", "OpenPseudo", "OpenDup", "SorterOpen"})
_OTHER_ACCESS = frozenset({"IntegrityCk", "TableLock", "Program"})  # reach tables with no cursor of the above
_P2_IS_REGISTER = 0x10  # in p5: the root page is in a register, not in p2

_LOWER = str.maketrans("ABCDEFGHIJKLMNOPQRSTUVWXYZ", "abcdefghijklmnopqrstuvwxyz")


def fold(name: str) -> str:
    """`name` as SQLite compares names: ASCII letters in lower case, every other character as it is."""
    return name.translate(_LOWER)


def union(a: Sources, b: Sources) -> Sources:
    """The record of data from both: unknown if either is."""
    return None if a is None or b is None else a | b


def from_names(names: object) -> Sources:
    """The record a list of table names holds; None, not known, for anything else."""
    if not isinstance(names, list) or not all(isinstance(n, str) for n in names):
        return None
    return frozenset(names)


def sources_json(sources: Sources) -> str | None:
    """`sources` as a JSON list in a stable order; None if they are not known."""
    return None if sources is None else json.dumps(sorted(sources))


class _Unrecognised(Exception):
    """The query reaches something that cannot be mapped to a table. The message holds no value from the query."""


def sources_of(con: sqlite3.Connection, query: str, authorize: Authorizer, recorded: Recorded) -> Sources:
    """The sources of `query`'s rows, compiled (not run) on `con`, which must be inside the transaction that read
    them, so that no table it names can have been replaced. `authorize` is the authorizer `query` ran under; `con` is
    left without one. None for anything unrecognised, and for any failure: this never raises an `Exception`."""
    try:
        found: set[str] = set()
        for database, table in _opened(con, query, authorize):
            if database == _TELEMETRY:
                found.add(table)
                continue
            record = recorded(table)
            if record is None:
                return None
            found |= record
        return frozenset(found)
    except _Unrecognised as e:
        log.info("table sources unknown: %s", e)
    except Exception:
        log.warning("table sources unknown: they could not be established", exc_info=True)
    return None


def _opened(con: sqlite3.Connection, query: str, authorize: Authorizer) -> set[tuple[str, str]]:
    """The (database, table) pairs compiling `query` shows it reads."""
    asked: set[tuple[str | None, str, str, str | None]] = set()

    def record(action, arg1, arg2, database, source):
        if action == sqlite3.SQLITE_READ:
            asked.add((database, arg1 or "", arg2 or "", source))
        return authorize(action, arg1, arg2, database, source)

    con.set_authorizer(record)
    # The program quotes the query's blobs, which need not be text.
    text_factory, con.text_factory = con.text_factory, lambda value: value.decode(errors="replace")
    try:
        program = con.execute(f"EXPLAIN {query}").fetchall()  # compiles it, and runs nothing
    finally:
        con.text_factory = text_factory
        con.set_authorizer(None)

    databases = {number: name for number, name, _ in con.execute("PRAGMA database_list")}
    roots: dict[tuple[str, int], str] = {}  # (database, root page) of each table and index -> its table
    tables: set[tuple[str, str]] = set()  # (database, name) of each table and view
    views: set[tuple[str, str]] = set()
    for database in (_TELEMETRY, _OWN):
        for kind, name, table, root in con.execute(
                f"SELECT type, name, tbl_name, rootpage FROM {database}.sqlite_master"):
            if kind in ("table", "view"):
                tables.add((database, fold(name)))
            if kind == "view":
                views.add((database, fold(name)))
            if kind in ("table", "index") and root:
                roots[database, root] = fold(table)

    opened: set[tuple[str, str]] = set()
    for _, opcode, _, p2, p3, _, p5, _ in program:
        if opcode in _DATABASE_OPENS:
            database = databases.get(p3, "")
            if p5 & _P2_IS_REGISTER or (database, p2) not in roots:  # a schema table's root page is not listed
                raise _Unrecognised(f"{opcode} of no known table")
            opened.add((database, roots[database, p2]))
        elif opcode in _OTHER_ACCESS or ("open" in opcode.lower() and opcode not in _TRANSIENT_OPENS):
            raise _Unrecognised(f"the program uses {opcode}")  # VOpen: a virtual table or table function

    for database, name, column, source in asked:
        # No database is named when only a table's rows matter, and then the name may also be a CTE's.
        within = (_TELEMETRY, _OWN) if database is None else (fold(database),)
        named = {(d, fold(name)) for d in within} & tables
        if not named and (column or fold(name).startswith("sqlite_")):
            raise _Unrecognised("a read of no known table")
        opened |= named
        if source is not None:  # a view (or a CTE, which `views` does not hold) the read was made through
            opened |= {(d, fold(source)) for d in (_TELEMETRY, _OWN)} & views

    for database, table in opened:
        if table.startswith("sqlite_") or (database == _OWN and (database, table) in views):
            raise _Unrecognised("a schema table or a view of the User's")
    return opened
