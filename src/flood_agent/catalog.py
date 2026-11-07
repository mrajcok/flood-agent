"""The Table Catalog: the one authority on whether a Result Table or Saved Table is live.

It owns the registry of those tables (and of Lookup Tables) in the app database. A table is live when
it has not been dropped AND its creation time plus its Retention is later than now (the injected clock),
worked out on every read, so nothing depends on a sweep having run. Retention is stored on the row when
the table is registered, so a later change to the configured default does not touch existing tables.
"""

import json
import sqlite3
import time
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable, Iterable, Literal, NamedTuple

from flood_agent import lookup_tables, queries, table_names

SCHEMA = """
CREATE TABLE IF NOT EXISTS result_tables (
  name TEXT PRIMARY KEY,
  conversation_id TEXT NOT NULL REFERENCES conversations(id),
  turn_id INTEGER NOT NULL,
  created_at INTEGER NOT NULL,
  title TEXT NOT NULL DEFAULT '',
  columns TEXT NOT NULL DEFAULT '[]',
  request TEXT NOT NULL DEFAULT '',
  expired INTEGER NOT NULL DEFAULT 0,
  truncated_to INTEGER,
  retention_ms INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS lookup_tables (
  name TEXT PRIMARY KEY,
  created_at INTEGER NOT NULL,
  columns TEXT NOT NULL DEFAULT '[]',
  row_count INTEGER NOT NULL DEFAULT 0
);
"""

DAY_MS = 86_400_000
ORPHAN_AGE = timedelta(hours=1)


@dataclass(frozen=True)
class RetentionPolicy:
    """The Retention rules, in days: what a new Result Table gets (the configured default), what a
    renamed Result Table is raised to (ADR 0004), and the longest Retention a user may pick."""

    result_days: float
    saved_days: float = 30
    max_days: float = 3650


class TableExpired(Exception):
    """The table is registered but no longer live. Deliberately not a KeyError, so a route that turns
    KeyError (unknown table) into 404 still lets the app-wide 410 handler run."""


@dataclass(frozen=True)
class Table:
    """A live Result Table or Saved Table, as the registry describes it (no row values)."""

    name: str
    kind: Literal["result", "saved"]
    conversation_id: str
    turn_id: int
    title: str
    columns: list[dict]
    request: str
    truncated_to: int | None  # the row cap the table was cut to, or None if it is complete
    retention_ms: int
    expires_in_ms: int  # from now

    @property
    def retention_days(self) -> float:
        return self.retention_ms / DAY_MS

    def to_json(self) -> dict:
        return {"name": self.name, "retention_days": self.retention_days, "expires_in_ms": self.expires_in_ms}


class Owned(NamedTuple):
    """The live tables a Conversation or one turn owns: Result Tables (still named `rt_*`) and Saved Tables."""

    result: list[Table]
    saved: list[Table]


def _now() -> int:
    return int(time.time() * 1000)


class CatalogView:
    """The read-only Table Catalog: every read, no write. It has no write operations and opens the app
    database in SQLite read-only mode, so the guarantee holds at runtime as well as in the types. The MCP
    server uses this (the app database must already exist)."""

    def __init__(
        self, app_db: Path, clock: Callable[[], int] = _now, telemetry_tables: Iterable[str] = (),
        results_db: Path | None = None, _read_only: bool = True,
    ):
        """`clock` returns epoch ms; `telemetry_tables` are the Telemetry Database table names;
        `results_db` is where Lookup Tables' rows are kept (needed only by the Lookup Table operations)."""
        self.path = Path(app_db)
        self.results_db = Path(results_db) if results_db is not None else None
        self._now = clock
        self._read_only = _read_only
        self.telemetry_tables = frozenset(t.lower() for t in telemetry_tables)

    def _connect(self) -> sqlite3.Connection:
        if self._read_only:
            con = sqlite3.connect(f"file:{self.path}?mode=ro", uri=True, timeout=5)
        else:
            con = sqlite3.connect(self.path)
        con.row_factory = sqlite3.Row
        return con

    def lookup_names(self) -> set[str]:
        """Every registered Lookup Table (the registry, not the `lookup_` prefix, decides: ADR 0003)."""
        with closing(self._connect()) as con:
            return {r[0] for r in con.execute("SELECT name FROM lookup_tables")}

    def kind_of(self, name: str) -> Literal["telemetry", "result", "saved", "lookup"] | None:
        """The kind of `name` (any case), or None if it is not a table here or no longer live."""
        wanted = name.lower()
        if wanted in self.telemetry_tables:
            return "telemetry"
        if wanted in {n.lower() for n in self.lookup_names()}:
            return "lookup"
        try:
            return self[wanted].kind
        except (KeyError, TableExpired):
            return None

    def lookups(self) -> list[dict]:
        """The Lookup Tables registry: name, created time, columns and row count, sorted by name."""
        with closing(self._connect()) as con:
            return [{**dict(r), "columns": json.loads(r["columns"])} for r in con.execute(
                "SELECT name, created_at, columns, row_count FROM lookup_tables ORDER BY name")]

    def listing(self) -> dict:
        """The Tables dialog data: names by kind (each sorted), and each live Result or Saved Table's
        Retention in days and time left in ms."""
        live = [self[n] for n in self.names()]
        return {
            "telemetry": sorted(self.telemetry_tables),
            "lookup": sorted(self.lookup_names()),
            "saved": [t.name for t in live if t.kind == "saved"],
            "result": [t.name for t in live if t.kind == "result"],
            "retention_days": {t.name: t.retention_days for t in live},
            "expires_in_ms": {t.name: t.expires_in_ms for t in live},
        }

    def owned(self, conversation_id: str, turn_id: int | None = None) -> Owned:
        """The live tables a Conversation (or just its turn `turn_id`) made, by the turn recorded when
        each was registered."""
        tables = [t for t in self.tables_of(conversation_id) if turn_id is None or t.turn_id == turn_id]
        return Owned([t for t in tables if t.kind == "result"], [t for t in tables if t.kind == "saved"])

    def readable(self) -> set[str]:
        """Every table a query may read (ADR 0003): all live Result and Saved Tables, from any Conversation,
        and every Lookup Table."""
        return set(self.names()) | self.lookup_names()

    def _table(self, row: sqlite3.Row) -> Table:
        return Table(
            name=row["name"],
            kind="result" if table_names.is_result_table_name(row["name"]) else "saved",
            conversation_id=row["conversation_id"], turn_id=row["turn_id"], title=row["title"],
            columns=json.loads(row["columns"]), request=row["request"], truncated_to=row["truncated_to"],
            retention_ms=row["retention_ms"], expires_in_ms=row["created_at"] + row["retention_ms"] - self._now(),
        )

    @staticmethod
    def _is_live(row: sqlite3.Row, now: int) -> bool:
        return not row["expired"] and row["created_at"] + row["retention_ms"] > now

    def __getitem__(self, name: str) -> Table:
        """The live Result or Saved Table `name` (any case). KeyError if no such table is registered;
        `TableExpired` if it is registered but no longer live."""
        with closing(self._connect()) as con:
            row = con.execute("SELECT * FROM result_tables WHERE name=?", (name.lower(),)).fetchone()
        if row is None:
            raise KeyError(name)
        if not self._is_live(row, self._now()):
            raise TableExpired(f"Result Table {name} has expired.")
        return self._table(row)

    def __contains__(self, name: object) -> bool:
        if not isinstance(name, str):
            return False
        with closing(self._connect()) as con:
            row = con.execute("SELECT * FROM result_tables WHERE name=?", (name.lower(),)).fetchone()
        return row is not None and self._is_live(row, self._now())

    def live_among(self, names: Iterable[str]) -> set[str]:
        """The subset of `names` that are live Result or Saved Tables (one query per 500 names)."""
        names, now, out = list({n.lower() for n in names}), self._now(), set()
        with closing(self._connect()) as con:
            for i in range(0, len(names), 500):
                chunk = names[i:i + 500]
                marks = ",".join("?" * len(chunk))
                out.update(r["name"] for r in con.execute(
                    f"SELECT * FROM result_tables WHERE name IN ({marks})", chunk) if self._is_live(r, now))
        return out

    def tables_of(self, conversation_id: str) -> list[Table]:
        """The live Result and Saved Tables a Conversation made, in the order they were created."""
        now = self._now()
        with closing(self._connect()) as con:
            rows = con.execute(
                "SELECT * FROM result_tables WHERE conversation_id=? ORDER BY created_at, name", (conversation_id,))
            return [self._table(r) for r in rows if self._is_live(r, now)]

    def names(self) -> list[str]:
        """Names of every live Result and Saved Table, across Conversations, sorted."""
        now = self._now()
        with closing(self._connect()) as con:
            return [r["name"] for r in con.execute("SELECT * FROM result_tables ORDER BY name") if self._is_live(r, now)]

    # --- Lookup Tables: the registry, not the `lookup_` prefix, makes a table one (ADR 0003) ----------

    def _is_lookup(self, name: str) -> bool:
        with closing(self._connect()) as con:
            return con.execute("SELECT 1 FROM lookup_tables WHERE name=?", (name.lower(),)).fetchone() is not None

    def _results_db(self) -> Path:
        if self.results_db is None:
            raise RuntimeError("This Catalog was built without a results database.")
        return self.results_db

    def lookup_text(self, name: str, sep: str) -> dict:
        """A Lookup Table's rows as text for the edit dialog, header line first, separated by `sep` (a key
        of `lookup_tables.SEPARATORS`). KeyError if `name` is not a Lookup Table."""
        if not self._is_lookup(name):
            raise KeyError(name)
        name = name.lower()
        return {"name": name, "text": lookup_tables.format_rows(lookup_tables.read_table(self._results_db(), name), sep)}


class Catalog(CatalogView):
    """The writer: the view's reads plus registration and renaming. Creates the registry tables if missing."""

    def __init__(
        self, app_db: Path, clock: Callable[[], int] = _now, default_retention_ms: int = DAY_MS,
        telemetry_tables: Iterable[str] = (), results_db: Path | None = None,
        on_rename: Callable[[sqlite3.Connection, str, str], None] | None = None,
    ):
        """`default_retention_ms` is the Retention a new Result Table is given. `on_rename(con, old, new)` is
        the store's hook: it runs in the same app-database transaction as the registry rename, to update
        whatever else shows the old name (Displays and Chart Specs), so the Catalog never knows their schema."""
        super().__init__(app_db, clock, telemetry_tables, results_db, _read_only=False)
        self.default_retention_ms = default_retention_ms
        self.policy = RetentionPolicy(result_days=default_retention_ms / DAY_MS)
        self._on_rename = on_rename
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self._connect()) as con, con:
            con.executescript(SCHEMA)

    def register(
        self, name: str, conversation_id: str, turn_id: int, title: str = "", columns: list[dict] | None = None,
        request: str = "", truncated_to: int | None = None,
    ) -> None:
        """Record a new Result Table made by `turn_id`, with the current default Retention."""
        now = self._now()
        with closing(self._connect()) as con, con:
            con.execute(
                "INSERT OR IGNORE INTO result_tables(name, conversation_id, turn_id, created_at, title, columns,"
                " request, truncated_to, retention_ms) VALUES (?,?,?,?,?,?,?,?,?)",
                (name.lower(), conversation_id, turn_id, now, title, json.dumps(columns or []), request, truncated_to,
                 self.default_retention_ms),
            )
            con.execute("UPDATE conversations SET last_activity_at=? WHERE id=?", (now, conversation_id))

    def _drop_physical(self, names: Iterable[str]) -> None:
        names = list(names)
        if not names or self.results_db is None or not self.results_db.exists():
            return
        with closing(sqlite3.connect(self.results_db)) as con, con:
            for n in names:
                con.execute(f"DROP TABLE IF EXISTS {table_names.quote(n)}")

    def _mark_expired(self, names: list[str]) -> None:
        with closing(self._connect()) as con, con:
            con.executemany("UPDATE result_tables SET expired=1 WHERE name=?", [(n,) for n in names])

    def drop_owned(self, conversation_id: str, turn_id: int | None = None) -> list[str]:
        """Drop the Result Tables a Conversation (or one turn) owns that are still named `rt_*`, never
        its Saved Tables. They stay registered as expired. Returns the names dropped."""
        names = [t.name for t in self.owned(conversation_id, turn_id).result]
        self._drop_physical(names)
        self._mark_expired(names)
        return names

    def delete(self, name: str) -> None:
        """Delete a Lookup, Saved or Result Table. A Saved or Result Table stays registered as expired,
        so Displays of it render as expired (ADR 0004). ValueError for a Telemetry table; KeyError for any
        name that is not a live table of those kinds."""
        kind = self.kind_of(name)
        if kind == "telemetry":
            raise ValueError(f"{name} is a Telemetry Database table and cannot be deleted.")
        if kind is None:
            raise KeyError(name)
        name = name.lower()
        self._drop_physical([name])
        if kind == "lookup":
            with closing(self._connect()) as con, con:
                con.execute("DELETE FROM lookup_tables WHERE name=?", (name,))
        else:
            self._mark_expired([name])

    def update(self, name: str, new_name: str | None = None, retention_days: float | None = None) -> Table:
        """Rename and/or set the Retention of a live Result or Saved Table; returns the new record.

        The new name is lower-cased (ADR 0005); one equal to the current name changes nothing. A rename
        of an `rt_*` table raises its Retention to at least the Saved Table default (ADR 0004), unless a
        Retention is given. ValueError, changing nothing, for a name that is taken (even by a dropped
        table), reserved, not an identifier or a Telemetry table's, or a Retention outside (0, cap].
        KeyError if unknown, `TableExpired` if no longer live."""
        table = self[name]
        retention_ms = None
        if retention_days is not None:
            if not 0 < retention_days <= self.policy.max_days:  # also rejects nan
                raise ValueError(f"Retention must be more than 0 and at most {self.policy.max_days:g} days.")
            retention_ms = max(1, round(retention_days * DAY_MS))
        new = new_name.lower() if new_name is not None else None
        if new is not None and new != table.name:
            if retention_ms is None and table.kind == "result":
                retention_ms = max(table.retention_ms, round(self.policy.saved_days * DAY_MS))
            self._rename(table.name, new, retention_ms)
        elif retention_ms is not None:
            with closing(self._connect()) as con, con:
                con.execute("UPDATE result_tables SET retention_ms=? WHERE name=?", (retention_ms, table.name))
        return self[new or table.name]

    def _check_new_name(self, new: str) -> None:
        if not table_names.is_identifier(new):
            raise ValueError(f"A table name must {table_names.RULE}.")
        if table_names.is_reserved(new):
            raise ValueError("A table name cannot start with rt_ or lookup_; those are reserved.")
        if new in self.telemetry_tables:
            raise ValueError(f"{new} is the name of a Telemetry Database table.")
        with closing(self._connect()) as con:
            taken = con.execute("SELECT 1 FROM result_tables WHERE name=?", (new,)).fetchone()
        if taken:  # includes a dropped table: it stays registered so its Displays still render as expired
            raise ValueError(f"A table named {new} already exists.")

    def _rename(self, old: str, new: str, retention_ms: int | None) -> None:
        """Rename in the results database, then in the app database. These are two files, so if the
        second step fails the first is undone."""
        self._check_new_name(new)
        queries.rename_table(self._results_db(), old, new)  # ValueError if `new` is taken there
        try:
            with closing(self._connect()) as con, con:
                con.execute("UPDATE result_tables SET name=? WHERE name=?", (new, old))
                if retention_ms is not None:
                    con.execute("UPDATE result_tables SET retention_ms=? WHERE name=?", (retention_ms, new))
                if self._on_rename:
                    self._on_rename(con, old, new)
        except sqlite3.IntegrityError:
            queries.rename_table(self._results_db(), new, old)
            raise ValueError(f"A table named {new} already exists.")
        except BaseException:
            queries.rename_table(self._results_db(), new, old)
            raise

    def _register_lookup(self, name: str, parsed: lookup_tables.ParsedRows) -> dict:
        # The registry is in the app database, a separate file, so it cannot join the results-database
        # transaction. A crash between the two leaves a table the registry does not describe (its row
        # count or columns stale, or a new table unregistered and so unreadable) until it is added again.
        with closing(self._connect()) as con, con:
            con.execute(
                "INSERT INTO lookup_tables(name, created_at, columns, row_count) VALUES (?,?,?,?) "
                "ON CONFLICT(name) DO UPDATE SET columns=excluded.columns, row_count=excluded.row_count",
                (name, self._now(), json.dumps(parsed.columns), len(parsed.rows)))
        return {"name": name, "row_count": len(parsed.rows), "columns": parsed.columns}

    def add_lookup(self, suffix: str, text: str, overwrite: bool = False) -> dict:
        """Create `lookup_<suffix>` (lower-cased) from pasted rows. A name another table already uses is
        refused (`InvalidLookupTable`), unless it is a Lookup Table and `overwrite` is set. Never expires."""
        name = lookup_tables.table_name(suffix)
        parsed = lookup_tables.parse_rows(text)
        registered = self._is_lookup(name)
        try:
            lookup_tables.write_table(self._results_db(), name, parsed, replace=registered and overwrite)
        except lookup_tables.TableExists:
            raise lookup_tables.InvalidLookupTable(
                f"A table named {name} already exists. Tick Overwrite existing table to replace it."
                if registered else f"A table named {name} already exists and is not a Lookup Table."
            )
        return self._register_lookup(name, parsed)

    def replace_lookup(self, name: str, text: str) -> dict:
        """Replace a Lookup Table's rows (and columns) with edited text; its name stays. KeyError if `name`
        is not a Lookup Table; `InvalidLookupTable`, changing nothing, for a bad paste."""
        if not self._is_lookup(name):
            raise KeyError(name)
        name = name.lower()
        parsed = lookup_tables.parse_rows(text)
        lookup_tables.write_table(self._results_db(), name, parsed, replace=True)
        return self._register_lookup(name, parsed)

    def sweep(self) -> None:
        """Drop the physical tables of expired tables (they stay registered, as dropped), and any `rt_*`
        table in the results database that has no registry row and whose generated name is over an hour
        old (left by a turn cancelled while its tool call was waiting). Nothing correct depends on this."""
        if self.results_db is None or not self.results_db.exists():
            return
        now = self._now()
        # The generated name holds local time (queries.py), so compare on that basis.
        cutoff = datetime.fromtimestamp(now / 1000) - ORPHAN_AGE
        with closing(self._connect()) as app, closing(sqlite3.connect(self.results_db)) as results:
            rows = app.execute("SELECT name, created_at, retention_ms, expired FROM result_tables").fetchall()
            registered = {r["name"] for r in rows}
            due = [r["name"] for r in rows if not r["expired"] and r["created_at"] + r["retention_ms"] <= now]
            physical = [r[0] for r in results.execute("SELECT name FROM sqlite_master WHERE type='table'")]
            orphans = [n for n in physical if table_names.GENERATED_RE.fullmatch(n) and n not in registered
                       and (_name_time(n) or cutoff) < cutoff]
            for n in due + orphans:
                results.execute(f"DROP TABLE IF EXISTS {table_names.quote(n)}")
            results.commit()
            with app:
                app.executemany("UPDATE result_tables SET expired=1 WHERE name=?", [(n,) for n in due])


def _name_time(name: str) -> datetime | None:
    """The local time embedded in a generated name `rt_<YYYYMMDD>_<HHMMSS>[_n]` (None if not a real time)."""
    try:
        return datetime.strptime(name[3:18], "%Y%m%d_%H%M%S")
    except ValueError:
        return None
