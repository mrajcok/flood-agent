"""Query execution against the Telemetry Database and Result Table access.

Telemetry Database: opened read-only. Result Tables: created via CTAS in a separate,
writable results database that is ATTACHed to the (read-only) telemetry connection.
"""

import re
import sqlite3
import time
import uuid
from pathlib import Path

RESULT_TABLE_RE = re.compile(r"^rt_[0-9a-f]{8}$")

DEFAULT_QUERY_TIMEOUT_S = 30.0
DEFAULT_MAX_ROWS = 1_000_000

_FORBIDDEN_WORDS = {
    "insert", "update", "delete", "drop", "create", "alter", "attach", "detach",
    "pragma", "vacuum", "reindex", "analyze", "begin", "commit",
    "rollback", "savepoint", "release",
}
_ONLY_SELECT = "Only a single read-only SELECT statement is allowed."


class QueryRejected(Exception):
    pass


def _scan(sql: str) -> tuple[list[str], list[int]]:
    """Tokenise just enough to see past comments and quoted text.

    Returns (unquoted words lower-cased, positions of every ';' outside comments/quotes).
    """
    words: list[str] = []
    semis: list[int] = []
    i, n = 0, len(sql)
    while i < n:
        c = sql[i]
        if sql.startswith("--", i):
            j = sql.find("\n", i)
            i = n if j < 0 else j + 1
        elif sql.startswith("/*", i):
            j = sql.find("*/", i + 2)
            i = n if j < 0 else j + 2
        elif c in "'\"`":
            i += 1
            while i < n:
                if sql[i] == c:
                    if sql.startswith(c, i + 1):  # doubled quote escape
                        i += 2
                        continue
                    break
                i += 1
            i += 1
        elif c == "[":
            j = sql.find("]", i)
            i = n if j < 0 else j + 1
        elif c == ";":
            semis.append(i)
            i += 1
        elif c.isalpha() or c == "_":
            j = i
            while j < n and (sql[j].isalnum() or sql[j] == "_"):
                j += 1
            words.append(sql[i:j].lower())
            i = j
        else:
            i += 1
    return words, semis


def validate_select(sql: str) -> str:
    """Accept a single read-only SELECT (optionally WITH ... SELECT). Returns the SQL
    without its one optional trailing semicolon. Raises QueryRejected otherwise."""
    words, semis = _scan(sql)
    if not words or words[0] not in ("select", "with"):
        raise QueryRejected(_ONLY_SELECT)
    if _FORBIDDEN_WORDS & set(words):
        raise QueryRejected(_ONLY_SELECT)
    if len(semis) > 1:
        raise QueryRejected(_ONLY_SELECT)
    if semis:
        # The only permitted semicolon is a trailing one (only comments/space may follow).
        tail_words, tail_semis = _scan(sql[semis[0] + 1 :])
        if tail_words or tail_semis:
            raise QueryRejected(_ONLY_SELECT)
        return sql[: semis[0]]
    return sql


def sanitize_error(message: str) -> str:
    """Strip quoted literals and numbers from an SQLite error before it reaches the LLM."""
    message = re.sub(r"'[^']*'?|\"[^\"]*\"?|`[^`]*`?", "<literal>", message)
    return re.sub(r"(?<![A-Za-z_])\d+(\.\d+)*", "<n>", message)


_ALLOWED_ACTIONS = {
    sqlite3.SQLITE_SELECT,
    sqlite3.SQLITE_READ,
    sqlite3.SQLITE_FUNCTION,
    sqlite3.SQLITE_RECURSIVE,
}
_RESULT_WRITES = {sqlite3.SQLITE_CREATE_TABLE, sqlite3.SQLITE_INSERT, sqlite3.SQLITE_UPDATE}


def _authorizer(action, arg1, arg2, dbname, source):
    """Defence in depth: the only writes permitted are creating/filling a table in the
    results database. No ATTACH, PRAGMA, DDL/DML elsewhere."""
    if action in _ALLOWED_ACTIONS:
        return sqlite3.SQLITE_OK
    if action in _RESULT_WRITES and dbname == "results":
        return sqlite3.SQLITE_OK
    return sqlite3.SQLITE_DENY


def open_connection(telemetry_db: Path, results_db: Path) -> sqlite3.Connection:
    """Read-only Telemetry Database connection with the results database attached and a
    restrictive authorizer installed."""
    con = sqlite3.connect(f"file:{telemetry_db}?mode=ro", uri=True)
    con.execute("ATTACH DATABASE ? AS results", (f"file:{results_db}?mode=rwc",))
    con.set_authorizer(_authorizer)
    return con


_TIMEOUT_GUIDANCE = (
    "The query exceeded the {s:g} second time limit and was aborted. "
    "Aggregate the data (GROUP BY), narrow the etime time range, or add filters."
)
_ROWCAP_GUIDANCE = (
    "The query would produce more than {n:,} rows, so no Result Table was created. "
    "Aggregate the data (GROUP BY) or add filters to reduce the row count."
)


def run_query(
    telemetry_db: Path,
    results_db: Path,
    sql: str,
    timeout_s: float = DEFAULT_QUERY_TIMEOUT_S,
    max_rows: int = DEFAULT_MAX_ROWS,
) -> dict:
    """Run the SQL as CTAS into the results database. Returns Query Metadata only.

    On failure the dict has a sanitised `error` (safe for the LLM) and, for SQLite errors,
    a `raw_error` that the caller must keep for the Trace and strip before the LLM.
    """
    try:
        select = validate_select(sql)
    except QueryRejected as e:
        return {"success": False, "error": str(e)}
    name = f"rt_{uuid.uuid4().hex[:8]}"
    con = open_connection(telemetry_db, results_db)
    deadline = time.monotonic() + timeout_s
    timed_out = False

    def _progress():
        nonlocal timed_out
        if time.monotonic() > deadline:
            timed_out = True
            return 1
        return 0

    try:
        con.set_progress_handler(_progress, 1000)
        con.execute(f"CREATE TABLE results.{name} AS SELECT * FROM (\n{select}\n) LIMIT {int(max_rows) + 1}")
        con.set_progress_handler(None, 0)
        con.set_authorizer(None)
        con.commit()
        count = con.execute(f"SELECT COUNT(*) FROM results.{name}").fetchone()[0]
        if count > max_rows:
            con.execute(f"DROP TABLE results.{name}")
            con.commit()
            return {"success": False, "error": _ROWCAP_GUIDANCE.format(n=max_rows)}
        cols = [
            {"name": r[1], "type": r[2] or "ANY"}
            for r in con.execute(f"PRAGMA results.table_info({name})")
        ]
    except sqlite3.Error as e:
        if timed_out:
            return {
                "success": False,
                "error": _TIMEOUT_GUIDANCE.format(s=timeout_s),
                "raw_error": str(e),
            }
        return {"success": False, "error": sanitize_error(str(e)), "raw_error": str(e)}
    finally:
        con.close()
    return {"success": True, "result_table": name, "columns": cols, "row_count": count}


def result_table_columns(results_db: Path, name: str) -> list[str] | None:
    if not RESULT_TABLE_RE.match(name) or not Path(results_db).exists():
        return None
    con = sqlite3.connect(f"file:{results_db}?mode=ro", uri=True)
    try:
        cols = [r[1] for r in con.execute(f"PRAGMA table_info({name})")]
    finally:
        con.close()
    return cols or None


def fetch_rows(results_db: Path, name: str, columns: list[str] | None = None, limit: int = 1000) -> dict:
    """Internal data path: rows of a Result Table as JSON-able data. Never an LLM tool."""
    all_cols = result_table_columns(results_db, name)
    if all_cols is None:
        raise KeyError(name)
    cols = [c for c in (columns or all_cols) if c in all_cols]
    select = ", ".join(f'"{c}"' for c in cols)
    con = sqlite3.connect(f"file:{results_db}?mode=ro", uri=True)
    try:
        rows = [list(r) for r in con.execute(f"SELECT {select} FROM {name} LIMIT ?", (limit,))]
    finally:
        con.close()
    return {"columns": cols, "rows": rows}
