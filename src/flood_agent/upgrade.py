"""The one-time startup upgrade to Notebooks: an app database from before them is emptied.

Result Tables then belonged to a Conversation turn (`conversation_id` and `turn_id` NOT NULL); now they may
belong to a Cell Run instead. The app has no users yet, so rather than migrate, every old table (Result, Saved
and Lookup) is dropped with its registry row, and so is every Conversation, and the schema is made afresh.
There is no general migration framework.
"""

import sqlite3
from contextlib import closing
from pathlib import Path

OLD_TABLES = ("displays", "messages", "turns", "conversations", "result_tables", "lookup_tables")


def is_pre_notebook(con: sqlite3.Connection) -> bool:
    """True if `result_tables` exists with the old NOT NULL Conversation owner."""
    return any(r["name"] == "conversation_id" and r["notnull"] for r in con.execute("PRAGMA table_info(result_tables)"))


def drop_pre_notebook_data(app_db: Path, results_db: Path) -> None:
    """If the app database is a pre-Notebook one, drop all its data and every table it registered."""
    if not app_db.exists():
        return
    with closing(sqlite3.connect(app_db)) as con:
        con.row_factory = sqlite3.Row
        if not is_pre_notebook(con):
            return
        have = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        registered = [r[0] for table in ("result_tables", "lookup_tables") if table in have
                      for r in con.execute(f"SELECT name FROM {table}")]
        if results_db.exists():
            with closing(sqlite3.connect(results_db)) as results, results:
                for name in registered:
                    results.execute(f'DROP TABLE IF EXISTS "{name.replace(chr(34), chr(34) * 2)}"')
        with con:
            for table in OLD_TABLES:
                con.execute(f"DROP TABLE IF EXISTS {table}")
