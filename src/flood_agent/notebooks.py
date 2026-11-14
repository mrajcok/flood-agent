"""Notebook storage: Notebooks, their ordered Cells (kind + Draft) and each Cell's latest Run.

Lives in the app database beside Conversations. A Cell has at most one Run, its latest: starting a new
Run replaces the old one (its messages, Displays and Trace) under a new id. AI and Markdown Cells exist so far (a Markdown Cell never has a Run).
A Run's Result Tables are the Catalog's, recorded against the `CellRun(cell_id, run_id)` that made them.
"""

import json
import re
import sqlite3
import time
import uuid
from contextlib import closing
from pathlib import Path
from typing import Callable

SCHEMA = """
CREATE TABLE IF NOT EXISTS notebooks (
  id TEXT PRIMARY KEY,
  title TEXT NOT NULL,
  created_at INTEGER NOT NULL,
  last_activity_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS cells (
  id TEXT PRIMARY KEY,
  notebook_id TEXT NOT NULL REFERENCES notebooks(id),
  position INTEGER NOT NULL,
  kind TEXT NOT NULL,
  draft TEXT NOT NULL,
  created_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS cells_notebook ON cells(notebook_id, position);
CREATE TABLE IF NOT EXISTS runs (
  id TEXT PRIMARY KEY,
  cell_id TEXT NOT NULL UNIQUE REFERENCES cells(id),
  status TEXT NOT NULL,
  prompt TEXT NOT NULL,
  settings TEXT NOT NULL,
  context_fingerprint TEXT NOT NULL,
  started_at INTEGER NOT NULL,
  number INTEGER NOT NULL DEFAULT 0,
  finished_at INTEGER,
  context_tokens INTEGER,
  trace BLOB,
  result TEXT
);
CREATE TABLE IF NOT EXISTS run_messages (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  run_id TEXT NOT NULL REFERENCES runs(id),
  body TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS run_displays (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  run_id TEXT NOT NULL REFERENCES runs(id),
  after_message_id INTEGER NOT NULL,
  kind TEXT NOT NULL,
  result_table TEXT,
  definition TEXT NOT NULL
);
"""

AI = "ai"
SQL = "sql"
MARKDOWN = "markdown"
MARKDOWN_NOTE_LABEL = "[Markdown note]"  # prefixes a ticked Markdown Cell's text in an AI Cell's context
DEFAULT_MAX_ROWS = 1000


def _now() -> int:
    return int(time.time() * 1000)


class NotebookStore:
    def __init__(self, path: Path, clock: Callable[[], int] = _now):
        self.path = Path(path)
        self._now = clock
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self._connect()) as con, con:
            con.executescript(SCHEMA)
            if not any(r["name"] == "number" for r in con.execute("PRAGMA table_info(runs)")):
                con.execute("ALTER TABLE runs ADD COLUMN number INTEGER NOT NULL DEFAULT 0")  # a database from before it
            if not any(r["name"] == "result" for r in con.execute("PRAGMA table_info(runs)")):
                con.execute("ALTER TABLE runs ADD COLUMN result TEXT")

    def _connect(self) -> sqlite3.Connection:
        con = sqlite3.connect(self.path)
        con.row_factory = sqlite3.Row
        return con

    # --- notebooks and cells ------------------------------------------------------------------------

    def create_notebook(self, title: str | None = None) -> str:
        """`title` defaults to "Notebook N", N being one more than the highest number in use."""
        notebook_id, now = uuid.uuid4().hex, self._now()
        with closing(self._connect()) as con, con:
            if title is None:
                used = [int(m[1]) for (t,) in con.execute("SELECT title FROM notebooks")
                        if (m := re.fullmatch(r"Notebook (\d+)", t))]
                title = f"Notebook {max(used, default=0) + 1}"
            con.execute("INSERT INTO notebooks(id, title, created_at, last_activity_at) VALUES (?,?,?,?)",
                        (notebook_id, title, now, now))
        return notebook_id

    def exists(self, notebook_id: str) -> bool:
        with closing(self._connect()) as con:
            return con.execute("SELECT 1 FROM notebooks WHERE id=?", (notebook_id,)).fetchone() is not None

    def title_of(self, notebook_id: str) -> str:
        with closing(self._connect()) as con:
            row = con.execute("SELECT title FROM notebooks WHERE id=?", (notebook_id,)).fetchone()
        if row is None:
            raise KeyError(notebook_id)
        return row[0]

    def list_notebooks(self) -> list[dict]:
        with closing(self._connect()) as con:
            return [dict(r) for r in con.execute(
                "SELECT id, title, created_at, last_activity_at FROM notebooks "
                "ORDER BY last_activity_at DESC, created_at DESC")]

    def add_ai_cell(self, notebook_id: str, draft: dict, after: str | None = None, before: str | None = None) -> str:
        return self.add_cell(notebook_id, AI, draft, after, before)

    def add_cell(self, notebook_id: str, kind: str, draft: dict, after: str | None = None,
                 before: str | None = None) -> str:
        """Insert a cell of this kind with this Draft after cell `after`, before cell `before`, or at the end.
        KeyError if the notebook (or `after`/`before` in it) is unknown."""
        cell_id, now = uuid.uuid4().hex, self._now()
        with closing(self._connect()) as con, con:
            if con.execute("SELECT 1 FROM notebooks WHERE id=?", (notebook_id,)).fetchone() is None:
                raise KeyError(notebook_id)
            if before is not None:
                row = con.execute("SELECT position FROM cells WHERE id=? AND notebook_id=?",
                                  (before, notebook_id)).fetchone()
                if row is None:
                    raise KeyError(before)
                position = row[0]
                con.execute("UPDATE cells SET position=position+1 WHERE notebook_id=? AND position>=?",
                            (notebook_id, position))
            elif after is None:
                position = con.execute("SELECT COALESCE(MAX(position), -1) + 1 FROM cells WHERE notebook_id=?",
                                       (notebook_id,)).fetchone()[0]
            else:
                row = con.execute("SELECT position FROM cells WHERE id=? AND notebook_id=?",
                                  (after, notebook_id)).fetchone()
                if row is None:
                    raise KeyError(after)
                position = row[0] + 1
                con.execute("UPDATE cells SET position=position+1 WHERE notebook_id=? AND position>=?",
                            (notebook_id, position))
            con.execute("INSERT INTO cells(id, notebook_id, position, kind, draft, created_at) VALUES (?,?,?,?,?,?)",
                        (cell_id, notebook_id, position, kind, json.dumps(draft), now))
            self._touch(con, notebook_id)
        return cell_id

    def move_cell(self, cell_id: str, direction: str) -> None:
        """Swap the cell with its neighbour above ("up") or below ("down"); at either end, nothing changes."""
        with closing(self._connect()) as con, con:
            row = con.execute("SELECT notebook_id, position FROM cells WHERE id=?", (cell_id,)).fetchone()
            if row is None:
                raise KeyError(cell_id)
            op, order = ("<", "DESC") if direction == "up" else (">", "ASC")
            other = con.execute(
                f"SELECT id, position FROM cells WHERE notebook_id=? AND position {op} ? ORDER BY position {order} LIMIT 1",
                (row["notebook_id"], row["position"])).fetchone()
            if other is None:
                return
            con.execute("UPDATE cells SET position=? WHERE id=?", (other["position"], cell_id))
            con.execute("UPDATE cells SET position=? WHERE id=?", (row["position"], other["id"]))
            self._touch(con, row["notebook_id"])

    def delete_cell(self, cell_id: str) -> None:
        """Remove a cell with its Run, messages and Displays. (Its tables are the Catalog's: see `Agent.delete_cell`.)"""
        with closing(self._connect()) as con, con:
            row = con.execute("SELECT notebook_id FROM cells WHERE id=?", (cell_id,)).fetchone()
            if row is None:
                raise KeyError(cell_id)
            for (run_id,) in con.execute("SELECT id FROM runs WHERE cell_id=?", (cell_id,)).fetchall():
                for table in ("run_displays", "run_messages"):
                    con.execute(f"DELETE FROM {table} WHERE run_id=?", (run_id,))
            con.execute("DELETE FROM runs WHERE cell_id=?", (cell_id,))
            con.execute("DELETE FROM cells WHERE id=?", (cell_id,))
            self._touch(con, row["notebook_id"])

    def edit_ai_cell(self, cell_id: str, **changes) -> None:
        self.edit_cell(cell_id, **changes)

    def edit_cell(self, cell_id: str, **changes) -> None:
        """Change fields of a cell's Draft (an AI Cell's prompt, model, effort, max_rows; a SQL Cell's sql,
        max_rows; a Markdown Cell's text, send_to_ai). The latest Run is untouched."""
        with closing(self._connect()) as con, con:
            row = con.execute("SELECT notebook_id, draft FROM cells WHERE id=?", (cell_id,)).fetchone()
            if row is None:
                raise KeyError(cell_id)
            draft = {**json.loads(row["draft"]), **changes}
            con.execute("UPDATE cells SET draft=? WHERE id=?", (json.dumps(draft), cell_id))
            self._touch(con, row["notebook_id"])

    def _touch(self, con: sqlite3.Connection, notebook_id: str) -> None:
        con.execute("UPDATE notebooks SET last_activity_at=? WHERE id=?", (self._now(), notebook_id))

    def cell(self, cell_id: str) -> dict:
        """A cell: {id, notebook_id, kind, draft, run}; KeyError if unknown."""
        with closing(self._connect()) as con:
            row = con.execute("SELECT * FROM cells WHERE id=?", (cell_id,)).fetchone()
            if row is None:
                raise KeyError(cell_id)
            return self._cell(con, row)

    def cells(self, notebook_id: str) -> list[dict]:
        """The notebook's cells in order, each with its Draft and its latest Run (None if it has not run)."""
        with closing(self._connect()) as con:
            return [self._cell(con, r) for r in con.execute(
                "SELECT * FROM cells WHERE notebook_id=? ORDER BY position", (notebook_id,))]

    def _cell(self, con: sqlite3.Connection, row: sqlite3.Row) -> dict:
        run = con.execute("SELECT * FROM runs WHERE cell_id=?", (row["id"],)).fetchone()
        return {"id": row["id"], "notebook_id": row["notebook_id"], "kind": row["kind"],
                "draft": json.loads(row["draft"]), "run": self._run(con, run) if run else None}

    @staticmethod
    def _run(con: sqlite3.Connection, run: sqlite3.Row) -> dict:
        messages = [json.loads(r[0]) for r in con.execute(
            "SELECT body FROM run_messages WHERE run_id=? ORDER BY id", (run["id"],))]
        return {
            "id": run["id"], "status": run["status"], "prompt": run["prompt"],
            "settings": json.loads(run["settings"]), "context_fingerprint": run["context_fingerprint"],
            "number": run["number"], "started_at": run["started_at"], "finished_at": run["finished_at"],
            "context_tokens": run["context_tokens"], "messages": messages,
            "result": json.loads(run["result"]) if run["result"] else None,
            "trace": json.loads(run["trace"]) if run["trace"] else None,
        }

    # --- runs -----------------------------------------------------------------------------------------

    def start_run(self, cell_id: str, prompt: str, settings: dict, fingerprint: str) -> str:
        """Replace the cell's latest Run with a new, running one; returns its id."""
        run_id, now = uuid.uuid4().hex, self._now()
        with closing(self._connect()) as con, con:
            notebook_id = con.execute("SELECT notebook_id FROM cells WHERE id=?", (cell_id,)).fetchone()[0]
            number = con.execute(  # the notebook's run counter: the highest number in use, plus one
                "SELECT COALESCE(MAX(r.number), 0) + 1 FROM runs r JOIN cells c ON c.id = r.cell_id"
                " WHERE c.notebook_id=?", (notebook_id,)).fetchone()[0]
            old = con.execute("SELECT id FROM runs WHERE cell_id=?", (cell_id,)).fetchone()
            if old:
                for table in ("run_displays", "run_messages"):
                    con.execute(f"DELETE FROM {table} WHERE run_id=?", (old["id"],))
                con.execute("DELETE FROM runs WHERE id=?", (old["id"],))
            con.execute(
                "INSERT INTO runs(id, cell_id, status, prompt, settings, context_fingerprint, started_at, number)"
                " VALUES (?,?,?,?,?,?,?,?)",
                (run_id, cell_id, "running", prompt, json.dumps(settings), fingerprint, now, number))
            self._touch(con, notebook_id)
        return run_id

    def last_settings(self, kind: str = AI) -> dict | None:
        """The settings of the most recently started Run of a cell of this kind in any notebook (an AI Cell's
        model, effort, max_rows; a SQL Cell's max_rows), or None."""
        with closing(self._connect()) as con:
            row = con.execute(
                "SELECT r.settings FROM runs r JOIN cells c ON c.id = r.cell_id WHERE c.kind=?"
                " ORDER BY r.started_at DESC, r.rowid DESC LIMIT 1", (kind,)).fetchone()
        return json.loads(row[0]) if row else None

    def save_result(self, run_id: str, result: dict) -> None:
        """A SQL Cell Run's outcome: what the status line, the error and the page need."""
        with closing(self._connect()) as con, con:
            con.execute("UPDATE runs SET result=? WHERE id=?", (json.dumps(result), run_id))

    def finish_run(self, run_id: str, status: str) -> None:
        with closing(self._connect()) as con, con:
            con.execute("UPDATE runs SET status=?, finished_at=? WHERE id=?", (status, self._now(), run_id))

    def status_of(self, run_id: str) -> str | None:
        with closing(self._connect()) as con:
            row = con.execute("SELECT status FROM runs WHERE id=?", (run_id,)).fetchone()
        return row[0] if row else None

    def add_message(self, run_id: str, message: dict) -> int:
        with closing(self._connect()) as con, con:
            return con.execute("INSERT INTO run_messages(run_id, body) VALUES (?,?)",
                               (run_id, json.dumps(message))).lastrowid or 0

    def add_step(self, run_id: str, items: list[tuple[dict, list[tuple]]]) -> tuple[list[int], list[list[int]]]:
        """Persist one LLM step atomically (see `ConversationStore.add_step`)."""
        ids: list[int] = []
        display_ids: list[list[int]] = []
        with closing(self._connect()) as con, con:
            for message, displays in items:
                mid = con.execute("INSERT INTO run_messages(run_id, body) VALUES (?,?)",
                                  (run_id, json.dumps(message))).lastrowid or 0
                ids.append(mid)
                display_ids.append([])
                for kind, table, definition in displays:
                    cur = con.execute(
                        "INSERT INTO run_displays(run_id, after_message_id, kind, result_table, definition)"
                        " VALUES (?,?,?,?,?)", (run_id, mid, kind, table, json.dumps(definition)))
                    display_ids[-1].append(int(cur.lastrowid or 0))
        return ids, display_ids

    def save_trace(self, run_id: str, trace: dict, context_tokens: int | None) -> None:
        with closing(self._connect()) as con, con:
            con.execute("UPDATE runs SET trace=?, context_tokens=? WHERE id=?",
                        (json.dumps(trace).encode("utf-8"), context_tokens, run_id))

    def transcript(self, run_id: str) -> list[dict]:
        """A Run's messages interleaved with its Displays, as `ConversationStore.transcript` gives them (no Trace):
        {"type": "message", "message"} and {"type": "display", "display_id", "kind", "result_table", **definition}."""
        displays = self.displays(run_id)
        with closing(self._connect()) as con:
            rows = con.execute("SELECT id, body FROM run_messages WHERE run_id=? ORDER BY id", (run_id,)).fetchall()
        out: list[dict] = []
        for row in rows:
            out.append({"type": "message", "message": json.loads(row["body"])})
            out += [{"type": "display", "display_id": d["id"],
                     **{k: v for k, v in d.items() if k not in ("id", "after_message_id")}}
                    for d in displays if d["after_message_id"] == row["id"]]
        return out

    def displays(self, run_id: str) -> list[dict]:
        """The Display definitions of a Run: {id, after_message_id, kind, result_table, **definition}."""
        with closing(self._connect()) as con:
            return [{"id": r["id"], "after_message_id": r["after_message_id"], "kind": r["kind"],
                     "result_table": r["result_table"], **json.loads(r["definition"])}
                    for r in con.execute("SELECT * FROM run_displays WHERE run_id=? ORDER BY id", (run_id,))]
