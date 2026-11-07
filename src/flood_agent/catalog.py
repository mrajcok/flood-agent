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
from pathlib import Path
from typing import Callable, Iterable, Literal

from flood_agent import lookup_tables, table_names

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


def _now() -> int:
    return int(time.time() * 1000)


class Catalog:
    def __init__(self, app_db: Path, clock: Callable[[], int] = _now, default_retention_ms: int = DAY_MS,
                 results_db: Path | None = None):
        """`clock` returns epoch ms; `default_retention_ms` is the Retention a new Result Table is given;
        `results_db` is where Lookup Tables' rows are kept (needed only by the Lookup Table operations)."""
        self.path = Path(app_db)
        self.results_db = Path(results_db) if results_db is not None else None
        self._now = clock
        self.default_retention_ms = default_retention_ms
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self._connect()) as con, con:
            con.executescript(SCHEMA)

    def _connect(self) -> sqlite3.Connection:
        con = sqlite3.connect(self.path)
        con.row_factory = sqlite3.Row
        return con

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
                (name, conversation_id, turn_id, now, title, json.dumps(columns or []), request, truncated_to,
                 self.default_retention_ms),
            )
            con.execute("UPDATE conversations SET last_activity_at=? WHERE id=?", (now, conversation_id))

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
        """The live Result or Saved Table `name`. KeyError if no such table is registered; `TableExpired`
        if it is registered but no longer live."""
        with closing(self._connect()) as con:
            row = con.execute("SELECT * FROM result_tables WHERE name=?", (name,)).fetchone()
        if row is None:
            raise KeyError(name)
        if not self._is_live(row, self._now()):
            raise TableExpired(f"Result Table {name} has expired.")
        return self._table(row)

    def __contains__(self, name: object) -> bool:
        if not isinstance(name, str):
            return False
        with closing(self._connect()) as con:
            row = con.execute("SELECT * FROM result_tables WHERE name=?", (name,)).fetchone()
        return row is not None and self._is_live(row, self._now())

    def live_among(self, names: Iterable[str]) -> set[str]:
        """The subset of `names` that are live Result or Saved Tables (one query per 500 names)."""
        names, now, out = list(set(names)), self._now(), set()
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

    def _results_db(self) -> Path:
        if self.results_db is None:
            raise RuntimeError("This Catalog was built without a results database.")
        return self.results_db

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

    def lookup_text(self, name: str, sep: str) -> dict:
        """A Lookup Table's rows as text for the edit dialog, header line first, separated by `sep` (a key
        of `lookup_tables.SEPARATORS`). KeyError if `name` is not a Lookup Table."""
        if not self._is_lookup(name):
            raise KeyError(name)
        name = name.lower()
        return {"name": name, "text": lookup_tables.format_rows(lookup_tables.read_table(self._results_db(), name), sep)}
