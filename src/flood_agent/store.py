"""App database: Conversations, turns, messages, Display definitions, Result Table ownership.

A separate SQLite file (not the Telemetry Database, not the results database). Displays are
stored as definitions only (kind + Result Table + options), never rows, so reopening a
Conversation re-renders them from their Result Tables.

Extension points for later tickets:
- `conversations.last_activity_at` (epoch ms) is touched on every write (Result Table expiry).
- `result_tables` records which Conversation owns each Result Table.
- `turns.trace` is a nullable BLOB reserved for the per-turn Trace.
"""

import json
import re
import sqlite3
import time
import uuid
from contextlib import closing
from pathlib import Path
from typing import Literal

SCHEMA = """
CREATE TABLE IF NOT EXISTS conversations (
  id TEXT PRIMARY KEY,
  title TEXT NOT NULL,
  created_at INTEGER NOT NULL,
  last_activity_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS turns (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  conversation_id TEXT NOT NULL REFERENCES conversations(id),
  user_message TEXT NOT NULL,
  created_at INTEGER NOT NULL,
  trace BLOB
);
CREATE TABLE IF NOT EXISTS messages (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  conversation_id TEXT NOT NULL REFERENCES conversations(id),
  turn_id INTEGER NOT NULL REFERENCES turns(id),
  body TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS messages_conv ON messages(conversation_id, id);
CREATE TABLE IF NOT EXISTS displays (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  conversation_id TEXT NOT NULL REFERENCES conversations(id),
  turn_id INTEGER NOT NULL REFERENCES turns(id),
  after_message_id INTEGER NOT NULL,
  kind TEXT NOT NULL,
  result_table TEXT,
  definition TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS displays_conv ON displays(conversation_id, id);
CREATE TABLE IF NOT EXISTS result_tables (
  name TEXT PRIMARY KEY,
  conversation_id TEXT NOT NULL REFERENCES conversations(id),
  created_at INTEGER NOT NULL,
  title TEXT NOT NULL DEFAULT '',
  columns TEXT NOT NULL DEFAULT '[]',
  request TEXT NOT NULL DEFAULT '',
  expired INTEGER NOT NULL DEFAULT 0,
  truncated_to INTEGER,
  retention_ms INTEGER
);
CREATE TABLE IF NOT EXISTS lookup_tables (
  name TEXT PRIMARY KEY,
  created_at INTEGER NOT NULL,
  columns TEXT NOT NULL DEFAULT '[]',
  row_count INTEGER NOT NULL DEFAULT 0
);
"""

# Columns added to result_tables after #8, for app databases created before then.
_RESULT_TABLE_MIGRATIONS = {
    "title": "TEXT NOT NULL DEFAULT ''",
    "columns": "TEXT NOT NULL DEFAULT '[]'",
    "request": "TEXT NOT NULL DEFAULT ''",
    "expired": "INTEGER NOT NULL DEFAULT 0",
    "truncated_to": "INTEGER",
    "retention_ms": "INTEGER",
}
_LOOKUP_TABLE_MIGRATIONS = {"row_count": "INTEGER NOT NULL DEFAULT 0"}

# Which of a Conversation's live Result Tables a delete also drops: none, those still named rt_*, or all.
TablesChoice = Literal["keep", "delete_rt", "delete_all"]


def _follow_rename(node, old: str, new: str):
    """A saved Chart Spec with every {"name": old} data reference pointing at `new`."""
    if isinstance(node, dict):
        if node == {"name": old}:
            return {"name": new}
        return {k: _follow_rename(v, old, new) for k, v in node.items()}
    if isinstance(node, list):
        return [_follow_rename(i, old, new) for i in node]
    return node


def _now() -> int:
    return int(time.time() * 1000)


class ConversationStore:
    def __init__(self, path: Path, clock=_now):
        """`clock` returns epoch milliseconds; injectable so tests control expiry."""
        self.path = Path(path)
        self._now = clock
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as con:
            con.executescript(SCHEMA)
            for table, migrations in (("result_tables", _RESULT_TABLE_MIGRATIONS),
                                      ("lookup_tables", _LOOKUP_TABLE_MIGRATIONS)):
                have = {r[1] for r in con.execute(f"PRAGMA table_info({table})")}
                for col, decl in migrations.items():
                    if col not in have:
                        con.execute(f"ALTER TABLE {table} ADD COLUMN {col} {decl}")

    def _connect(self) -> sqlite3.Connection:
        con = sqlite3.connect(self.path)
        con.row_factory = sqlite3.Row
        return con

    def _write(self, sql: str, params=(), conversation_id: str | None = None) -> int:
        with closing(self._connect()) as con, con:
            cur = con.execute(sql, params)
            if conversation_id:
                con.execute(
                    "UPDATE conversations SET last_activity_at=? WHERE id=?", (self._now(), conversation_id)
                )
            return cur.lastrowid or 0

    def create_conversation(self, title: str | None = None) -> str:
        """`title` defaults to "Conversation N", N being one more than the highest number in use."""
        cid, now = uuid.uuid4().hex, self._now()
        title = title or self._next_conversation_title()
        self._write(
            "INSERT INTO conversations(id, title, created_at, last_activity_at) VALUES (?,?,?,?)",
            (cid, title, now, now),
        )
        return cid

    def _next_conversation_title(self) -> str:
        with closing(self._connect()) as con:
            used = [int(m[1]) for (t,) in con.execute("SELECT title FROM conversations")
                    if (m := re.fullmatch(r"Conversation (\d+)", t))]
        return f"Conversation {max(used, default=0) + 1}"

    def rename_conversation(self, conversation_id: str, title: str) -> None:
        self._write("UPDATE conversations SET title=? WHERE id=?", (title, conversation_id))

    def exists(self, conversation_id: str) -> bool:
        with closing(self._connect()) as con:
            return con.execute("SELECT 1 FROM conversations WHERE id=?", (conversation_id,)).fetchone() is not None

    def list_conversations(self) -> list[dict]:
        with closing(self._connect()) as con:
            rows = con.execute(
                "SELECT id, title, created_at, last_activity_at FROM conversations "
                "ORDER BY last_activity_at DESC, created_at DESC"
            )
            return [dict(r) for r in rows]

    def _live_tables(self, con, conversation_id: str) -> list[sqlite3.Row]:
        return con.execute(
            "SELECT name FROM result_tables "
            "WHERE conversation_id=? AND expired=0", (conversation_id,)).fetchall()

    def _turn_owned(self, con, conversation_id: str, turn_id: int) -> list[sqlite3.Row]:
        """Live Result Tables a turn owns: a table belongs to the earliest turn whose messages
        mention its whole name (`rt_X` is not a mention of `rt_X_2`)."""
        bodies = con.execute(
            "SELECT turn_id, body FROM messages WHERE conversation_id=? ORDER BY id", (conversation_id,)).fetchall()

        shown = con.execute(
            "SELECT turn_id, result_table FROM displays WHERE conversation_id=? ORDER BY id", (conversation_id,)
        ).fetchall()  # a renamed table is only still named in Displays

        def first_mention(name: str):
            mention = re.compile(rf"(?<![A-Za-z0-9_]){re.escape(name)}(?![A-Za-z0-9_])")
            turns = [b["turn_id"] for b in bodies if mention.search(b["body"])]
            turns += [d["turn_id"] for d in shown if d["result_table"] == name]
            return min(turns, default=None)

        return [t for t in self._live_tables(con, conversation_id) if first_mention(t["name"]) == turn_id]

    @staticmethod
    def _choose(tables: list[sqlite3.Row], choice: TablesChoice) -> list[str]:
        """The names to delete under `choice`: keep, delete_rt (still named rt_*) or delete_all."""
        if choice == "delete_all":
            return [t["name"] for t in tables]
        if choice == "delete_rt":
            return [t["name"] for t in tables if t["name"].startswith("rt_")]
        return []

    def delete_preview(self, conversation_id: str, turn_id: int | None = None) -> dict:
        """Counts for the delete dialog: live Result Tables that would go under delete_all (`total`)
        and under delete_rt (`rt`). KeyError if the turn is not in the Conversation."""
        with closing(self._connect()) as con:
            if turn_id is None:
                tables = self._live_tables(con, conversation_id)
            else:
                if con.execute("SELECT 1 FROM turns WHERE id=? AND conversation_id=?",
                               (turn_id, conversation_id)).fetchone() is None:
                    raise KeyError(turn_id)
                tables = self._turn_owned(con, conversation_id, turn_id)
        return {"total": len(tables), "rt": len(self._choose(tables, "delete_rt"))}

    def delete_conversation(self, conversation_id: str, tables: TablesChoice = "keep") -> list[str]:
        """Remove a Conversation and everything it owns except the Result Tables the user keeps
        (they outlive it until they expire); returns the names of the tables to drop. Dropped tables
        stay registered as expired, so other Conversations' Displays of them render as expired."""
        with closing(self._connect()) as con, con:
            dropped = self._choose(self._live_tables(con, conversation_id), tables)
            con.executemany("UPDATE result_tables SET expired=1 WHERE name=?", [(n,) for n in dropped])
            for table in ("displays", "messages", "turns"):
                con.execute(f"DELETE FROM {table} WHERE conversation_id=?", (conversation_id,))
            con.execute("DELETE FROM conversations WHERE id=?", (conversation_id,))
        return dropped

    def delete_turn(self, conversation_id: str, turn_id: int, tables: TablesChoice = "keep") -> list[str]:
        """Remove one prompt/answer exchange; returns the Result Tables it created that are to be
        dropped (now expired). Displays elsewhere that show a dropped table render as expired."""
        with closing(self._connect()) as con, con:
            if con.execute("SELECT 1 FROM turns WHERE id=? AND conversation_id=?", (turn_id, conversation_id)).fetchone() is None:
                raise KeyError(turn_id)
            dropped = self._choose(self._turn_owned(con, conversation_id, turn_id), tables)
            con.executemany("UPDATE result_tables SET expired=1 WHERE name=?", [(n,) for n in dropped])
            for table in ("displays", "messages"):
                con.execute(f"DELETE FROM {table} WHERE turn_id=?", (turn_id,))
            con.execute("DELETE FROM turns WHERE id=?", (turn_id,))
        return dropped

    def start_turn(self, conversation_id: str, user_message: str) -> int:
        return self._write(
            "INSERT INTO turns(conversation_id, user_message, created_at) VALUES (?,?,?)",
            (conversation_id, user_message, self._now()),
            conversation_id,
        )

    def save_trace(self, turn_id: int, trace: dict) -> None:
        self._write("UPDATE turns SET trace=? WHERE id=?", (json.dumps(trace).encode("utf-8"), turn_id))

    def add_message(self, conversation_id: str, turn_id: int, message: dict) -> int:
        return self._write(
            "INSERT INTO messages(conversation_id, turn_id, body) VALUES (?,?,?)",
            (conversation_id, turn_id, json.dumps(message)),
            conversation_id,
        )

    def add_step(self, conversation_id: str, turn_id: int, items: list[tuple[dict, list[tuple]]]) -> list[int]:
        """Persist one LLM step atomically: each (message, displays) in order, where a display is
        (kind, result_table, definition) attached after that message. Either all rows are saved
        or none, so history never holds an assistant tool_calls message without its tool results.
        Returns the message ids."""
        ids = []
        with closing(self._connect()) as con, con:
            for message, displays in items:
                mid = con.execute(
                    "INSERT INTO messages(conversation_id, turn_id, body) VALUES (?,?,?)",
                    (conversation_id, turn_id, json.dumps(message)),
                ).lastrowid
                ids.append(mid)
                for kind, table, definition in displays:
                    con.execute(
                        "INSERT INTO displays(conversation_id, turn_id, after_message_id, kind, result_table,"
                        " definition) VALUES (?,?,?,?,?,?)",
                        (conversation_id, turn_id, mid, kind, table, json.dumps(definition)),
                    )
            con.execute("UPDATE conversations SET last_activity_at=? WHERE id=?", (self._now(), conversation_id))
        return ids

    def add_result_table(
        self, conversation_id: str, name: str, title: str = "", columns: list[dict] | None = None,
        request: str = "", truncated_to: int | None = None,
    ) -> None:
        """`truncated_to`: the row cap the table was cut to, or None if it is complete."""
        self._write(
            "INSERT OR IGNORE INTO result_tables(name, conversation_id, created_at, title, columns, request,"
            " truncated_to) VALUES (?,?,?,?,?,?,?)",
            (name, conversation_id, self._now(), title, json.dumps(columns or []), request, truncated_to),
            conversation_id,
        )

    def truncated_to(self, name: str) -> int | None:
        """The row cap the Result Table was cut to, or None if it is complete (or unknown)."""
        with closing(self._connect()) as con:
            row = con.execute("SELECT truncated_to FROM result_tables WHERE name=?", (name,)).fetchone()
            return row["truncated_to"] if row else None

    def result_tables(self, conversation_id: str) -> list[str]:
        """Names of the Conversation's live (unexpired) Result Tables."""
        return [t["name"] for t in self.live_result_tables(conversation_id)]

    def live_result_tables(self, conversation_id: str) -> list[dict]:
        """Name, title, column schema and originating request of live Result Tables (no row values)."""
        with closing(self._connect()) as con:
            rows = con.execute(
                "SELECT name, title, columns, request FROM result_tables "
                "WHERE conversation_id=? AND expired=0 ORDER BY created_at, name", (conversation_id,))
            return [{**dict(r), "columns": json.loads(r["columns"])} for r in rows]

    def all_live_result_table_names(self) -> list[str]:
        """Names of live (unexpired) Result Tables across all Conversations, deleted ones included, sorted."""
        with closing(self._connect()) as con:
            return [r[0] for r in con.execute("SELECT name FROM result_tables WHERE expired=0 ORDER BY name")]

    def due_for_expiry(self, ttl_ms: int) -> list[str]:
        """Live Result Tables created longer ago than the table's
        retention (`ttl_ms` unless the user chose one)."""
        now = self._now()
        with closing(self._connect()) as con:
            return [r[0] for r in con.execute(
                "SELECT name FROM result_tables "
                "WHERE expired=0 AND created_at <= ? - COALESCE(retention_ms, ?)", (now, ttl_ms))]

    def result_table_settings(self, name: str, default_retention_ms: int) -> dict | None:
        """The name and retention in ms of a Result Table, or None if unknown."""
        with closing(self._connect()) as con:
            row = con.execute("SELECT retention_ms FROM result_tables WHERE name=?", (name,)).fetchone()
        if row is None:
            return None
        return {"name": name, "retention_ms": row["retention_ms"] if row["retention_ms"] is not None else default_retention_ms}

    def has_result_table(self, name: str) -> bool:
        with closing(self._connect()) as con:
            return con.execute("SELECT 1 FROM result_tables WHERE name=?", (name,)).fetchone() is not None

    def rename_result_table(self, old: str, new: str) -> None:
        """Make `new` the table's one name, in the registry and in the Displays that show it."""
        with closing(self._connect()) as con, con:
            con.execute("UPDATE result_tables SET name=? WHERE name=?", (new, old))
            for row in con.execute("SELECT id, definition FROM displays WHERE result_table=?", (old,)).fetchall():
                definition = json.dumps(_follow_rename(json.loads(row["definition"]), old, new))
                con.execute("UPDATE displays SET result_table=?, definition=? WHERE id=?", (new, definition, row["id"]))

    def update_result_table(self, name: str, retention_ms: int | None = None) -> None:
        with closing(self._connect()) as con, con:
            if retention_ms is not None:
                con.execute("UPDATE result_tables SET retention_ms=? WHERE name=?", (retention_ms, name))

    def register_lookup_table(self, name: str, columns: list[str], row_count: int) -> None:
        """Record `name` as a Lookup Table (no Conversation, no expiry)."""
        self._write("INSERT INTO lookup_tables(name, created_at, columns, row_count) VALUES (?,?,?,?) "
                    "ON CONFLICT(name) DO UPDATE SET columns=excluded.columns, row_count=excluded.row_count",
                    (name, self._now(), json.dumps(columns), row_count))

    def lookup_tables(self) -> list[dict]:
        """The Lookup Tables registry: name, created time, columns and row count."""
        with closing(self._connect()) as con:
            return [{**dict(r), "columns": json.loads(r["columns"])} for r in con.execute(
                "SELECT name, created_at, columns, row_count FROM lookup_tables ORDER BY name")]

    def is_lookup_table(self, name: str) -> bool:
        """SQL table names ignore case, so this does too."""
        with closing(self._connect()) as con:
            return con.execute(
                "SELECT 1 FROM lookup_tables WHERE name=? COLLATE NOCASE", (name,)).fetchone() is not None

    def mark_expired(self, names: list[str]) -> None:
        with closing(self._connect()) as con, con:
            con.executemany("UPDATE result_tables SET expired=1 WHERE name=?", [(n,) for n in names])

    def is_expired(self, name: str) -> bool:
        with closing(self._connect()) as con:
            return con.execute(
                "SELECT 1 FROM result_tables WHERE name=? AND expired=1", (name,)).fetchone() is not None

    def expired_among(self, names) -> set[str]:
        """The subset of `names` that have expired (one query per 500 names)."""
        names, out = list(set(names)), set()
        with closing(self._connect()) as con:
            for i in range(0, len(names), 500):
                chunk = names[i:i + 500]
                marks = ",".join("?" * len(chunk))
                out.update(r[0] for r in con.execute(
                    f"SELECT name FROM result_tables WHERE expired=1 AND name IN ({marks})", chunk))
        return out

    def messages(self, conversation_id: str) -> list[dict]:
        """LLM-facing message history, in order."""
        with closing(self._connect()) as con:
            return [json.loads(r[0]) for r in con.execute(
                "SELECT body FROM messages WHERE conversation_id=? ORDER BY id", (conversation_id,))]

    def transcript(self, conversation_id: str) -> list[dict]:
        """Messages interleaved with Display definitions, for re-rendering the chat.

        Items: {"type": "message", "message": {...}} or {"type": "display", "kind", "result_table", **definition}.
        """
        with closing(self._connect()) as con:
            msgs = con.execute(
                "SELECT id, turn_id, body FROM messages WHERE conversation_id=? ORDER BY id", (conversation_id,)
            ).fetchall()
            displays = con.execute(
                "SELECT id, after_message_id, kind, result_table, definition FROM displays "
                "WHERE conversation_id=? ORDER BY id", (conversation_id,)
            ).fetchall()
            traces = {r["id"]: r["trace"] for r in con.execute(
                "SELECT id, trace FROM turns WHERE conversation_id=? AND trace IS NOT NULL", (conversation_id,))}
            last_of_turn = {r["turn_id"]: r["last"] for r in con.execute(
                "SELECT turn_id, MAX(id) AS last FROM messages WHERE conversation_id=? GROUP BY turn_id",
                (conversation_id,))}
        trace_after = {last_of_turn[t]: json.loads(b) for t, b in traces.items() if t in last_of_turn}
        out: list[dict] = []
        for m in msgs:
            out.append({"type": "message", "turn_id": m["turn_id"], "message": json.loads(m["body"])})
            for d in displays:
                if d["after_message_id"] == m["id"]:
                    out.append({"type": "display", "kind": d["kind"], "result_table": d["result_table"],
                                **json.loads(d["definition"])})
            if m["id"] in trace_after:
                out.append({"type": "trace", "trace": trace_after[m["id"]]})
        return out
