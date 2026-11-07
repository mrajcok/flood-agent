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

from flood_agent import table_names

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
    def __init__(self, app_db: Path, clock: Callable[[], int] = _now, default_retention_ms: int = DAY_MS):
        """`clock` returns epoch ms; `default_retention_ms` is the Retention a new Result Table is given."""
        self.path = Path(app_db)
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
