"""App database: Conversations, turns, messages, Display definitions.

The registry of Result Tables and Lookup Tables lives in the same file but belongs to the Table
Catalog (`catalog.py`), which creates it; the store still reads it for deleting, renaming and sweeping.

A separate SQLite file (not the Telemetry Database, not the results database). Displays are
stored as definitions only (kind + Result Table + options), never rows, so reopening a
Conversation re-renders them from their Result Tables.

Extension points for later tickets:
- `conversations.last_activity_at` (epoch ms) is touched on every write (Result Table expiry).
- `turns.trace` is a nullable BLOB reserved for the per-turn Trace.
"""

import json
import re
import sqlite3
import time
import uuid
from contextlib import closing
from pathlib import Path
from typing import Literal, NamedTuple

from flood_agent import table_names

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
"""

# Whether deleting a Conversation or exchange also drops its live Result Tables (those still named
# rt_*). Its Saved Tables are always kept (ADR 0004).
TablesChoice = Literal["keep", "delete_rt"]


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



class Lifetime(NamedTuple):
    retention_ms: int
    expires_in_ms: int  # from now

class ConversationStore:
    def __init__(self, path: Path, clock=_now):
        """`clock` returns epoch milliseconds; injectable so tests control expiry."""
        self.path = Path(path)
        self._now = clock
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as con:
            con.executescript(SCHEMA)

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
        """The names to delete under `choice`: none for keep, the Result Tables for delete_rt."""
        if choice == "delete_rt":
            return [t["name"] for t in tables if table_names.is_result_table_name(t["name"])]
        return []

    def delete_preview(self, conversation_id: str, turn_id: int | None = None) -> dict:
        """Counts for the delete dialog: the live Result Tables that delete_rt would drop (`result`)
        and the live Saved Tables that are always kept (`saved`). KeyError if the turn is not in the
        Conversation."""
        with closing(self._connect()) as con:
            if turn_id is None:
                tables = self._live_tables(con, conversation_id)
            else:
                if con.execute("SELECT 1 FROM turns WHERE id=? AND conversation_id=?",
                               (turn_id, conversation_id)).fetchone() is None:
                    raise KeyError(turn_id)
                tables = self._turn_owned(con, conversation_id, turn_id)
        result = len(self._choose(tables, "delete_rt"))
        return {"result": result, "saved": len(tables) - result}

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

    def live_result_table_lifetimes(self) -> dict[str, "Lifetime"]:
        """Live Result Tables (and Saved Tables) across all Conversations, sorted by name, each with
        its retention and time left until it expires."""
        now = self._now()
        with closing(self._connect()) as con:
            return {r[0]: Lifetime(r[1], r[2] + r[1] - now) for r in con.execute(
                "SELECT name, retention_ms, created_at FROM result_tables WHERE expired=0 ORDER BY name")}

    def due_for_expiry(self) -> list[str]:
        """Registered tables not yet dropped whose Retention has run out."""
        now = self._now()
        with closing(self._connect()) as con:
            return [r[0] for r in con.execute(
                "SELECT name FROM result_tables WHERE expired=0 AND created_at + retention_ms <= ?", (now,))]

    @staticmethod
    def follow_rename(con: sqlite3.Connection, old: str, new: str) -> None:
        """The Catalog's rename hook: point every Display (and Chart Spec data reference) at `new`, on the
        Catalog's connection so it commits or rolls back with the registry rename."""
        for row in con.execute("SELECT id, definition FROM displays WHERE result_table=?", (old,)).fetchall():
            definition = json.dumps(_follow_rename(json.loads(row[1]), old, new))
            con.execute("UPDATE displays SET result_table=?, definition=? WHERE id=?", (new, definition, row[0]))

    def lookup_tables(self) -> list[dict]:
        """The Lookup Tables registry: name, created time, columns and row count."""
        with closing(self._connect()) as con:
            return [{**dict(r), "columns": json.loads(r["columns"])} for r in con.execute(
                "SELECT name, created_at, columns, row_count FROM lookup_tables ORDER BY name")]

    def lookup_table_names(self) -> list[str]:
        """Names of the registered Lookup Tables, sorted."""
        with closing(self._connect()) as con:
            return [r[0] for r in con.execute("SELECT name FROM lookup_tables ORDER BY name")]

    def unregister_lookup_table(self, name: str) -> None:
        """Forget a Lookup Table (SQL names ignore case, so this does too)."""
        self._write("DELETE FROM lookup_tables WHERE name=? COLLATE NOCASE", (name,))

    def is_lookup_table(self, name: str) -> bool:
        """SQL table names ignore case, so this does too."""
        with closing(self._connect()) as con:
            return con.execute(
                "SELECT 1 FROM lookup_tables WHERE name=? COLLATE NOCASE", (name,)).fetchone() is not None

    def mark_expired(self, names: list[str]) -> None:
        with closing(self._connect()) as con, con:
            con.executemany("UPDATE result_tables SET expired=1 WHERE name=?", [(n,) for n in names])

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
