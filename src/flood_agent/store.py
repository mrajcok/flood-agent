"""App database: Conversations, turns, messages, Display definitions.

The registry of Result Tables and Lookup Tables lives in the same file but belongs to the Table
Catalog (`catalog.py`). The store only supplies the Display rename hook (`follow_rename`), which
makes every Display follow a table's new name inside the Catalog's rename transaction.

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

    def has_turn(self, conversation_id: str, turn_id: int) -> bool:
        with closing(self._connect()) as con:
            return con.execute("SELECT 1 FROM turns WHERE id=? AND conversation_id=?",
                               (turn_id, conversation_id)).fetchone() is not None

    def delete_conversation(self, conversation_id: str) -> None:
        """Remove a Conversation with its turns, messages and Displays. Tables are the Catalog's."""
        with closing(self._connect()) as con, con:
            for table in ("displays", "messages", "turns"):
                con.execute(f"DELETE FROM {table} WHERE conversation_id=?", (conversation_id,))
            con.execute("DELETE FROM conversations WHERE id=?", (conversation_id,))

    def delete_turn(self, conversation_id: str, turn_id: int) -> None:
        """Remove one prompt/answer exchange (KeyError if it is not in the Conversation)."""
        with closing(self._connect()) as con, con:
            if con.execute("SELECT 1 FROM turns WHERE id=? AND conversation_id=?", (turn_id, conversation_id)).fetchone() is None:
                raise KeyError(turn_id)
            for table in ("displays", "messages"):
                con.execute(f"DELETE FROM {table} WHERE turn_id=?", (turn_id,))
            con.execute("DELETE FROM turns WHERE id=?", (turn_id,))

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

    @staticmethod
    def follow_rename(con: sqlite3.Connection, old: str, new: str) -> None:
        """The Catalog's rename hook: point every Display (and Chart Spec data reference) at `new`, on the
        Catalog's connection so it commits or rolls back with the registry rename."""
        for row in con.execute("SELECT id, definition FROM displays WHERE result_table=?", (old,)).fetchall():
            definition = json.dumps(_follow_rename(json.loads(row[1]), old, new))
            con.execute("UPDATE displays SET result_table=?, definition=? WHERE id=?", (new, definition, row[0]))

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
