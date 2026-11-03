"""Query execution against the Telemetry Database and Result Table access.

Telemetry Database: opened read-only. Result Tables: created via CTAS in a separate,
writable results database that is ATTACHed to the (read-only) telemetry connection.
"""

import ipaddress
import re
import sqlite3
import time
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

# <title slug>_<YYYYMMDD>_<HHMMSS>[_<n>]: lower-case letters, digits and underscores only, starting
# with a letter, so the name is a valid unquoted identifier in SQLite and Ocient.
RESULT_TABLE_RE = re.compile(r"^[a-z][a-z0-9_]{0,79}_\d{8}_\d{6}(?:_\d+)?$")
_SLUG_MAX = 40


def _timestamp() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S")


def _slug(title: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "_", title.lower()).strip("_")[:_SLUG_MAX].strip("_") or "result"
    return f"t_{slug}" if slug[0].isdigit() else slug


def _free_name(con: sqlite3.Connection, title: str) -> str:
    """`<slug>_<date>_<time>`, with `_2`, `_3`... when that name is already taken."""
    base = f"{_slug(title)}_{_timestamp()}"
    taken = {r[0] for r in con.execute("SELECT name FROM results.sqlite_master WHERE name LIKE ? ESCAPE '\\'",
                                       (base.replace("_", "\\_") + "%",))}
    name, n = base, 1
    while name in taken:
        n += 1
        name = f"{base}_{n}"
    return name

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


def in_cidr(ip, cidr):
    """SQLite UDF: 1 if IPv4 `ip` is inside `cidr`, else 0 (NULL/invalid input -> NULL).
    Needs an Ocient mapping when the Telemetry Database moves there."""
    if ip is None or cidr is None:
        return None
    try:
        return int(ipaddress.IPv4Address(ip) in ipaddress.IPv4Network(cidr, strict=False))
    except ValueError:
        return None


_DENIAL_RE = re.compile(r"is prohibited|not authorized", re.I)
_QUALIFIED_RE = re.compile(r"\b(?:results|main|temp)\.\w+(?:\.\w+)?", re.I)


def sanitize_error(message: str) -> str:
    """Strip quoted literals and numbers from an SQLite error before it reaches the LLM.

    Authorizer denials ("access to results.<table>.col is prohibited", "not authorized") are
    replaced wholesale so no table or column name from a denied read (possibly another
    Conversation's) reaches the LLM; any other database-qualified reference is redacted."""
    if _DENIAL_RE.search(message):
        return "Access to a table or column was denied."
    message = _QUALIFIED_RE.sub("<table>", message)
    message = re.sub(r"'[^']*'?|\"[^\"]*\"?|`[^`]*`?", "<literal>", message)
    return re.sub(r"(?<![A-Za-z_])\d+(\.\d+)*", "<n>", message)


_ALLOWED_ACTIONS = {
    sqlite3.SQLITE_SELECT,
    sqlite3.SQLITE_READ,
    sqlite3.SQLITE_FUNCTION,
    sqlite3.SQLITE_RECURSIVE,
}
_RESULT_WRITES = {sqlite3.SQLITE_CREATE_TABLE, sqlite3.SQLITE_INSERT, sqlite3.SQLITE_UPDATE}


def _authorizer(allowed_tables: set[str] | None = None):
    """Single authorizer enforcing both policies (SQLite allows only one per connection):
    1. action allow-list: SELECT/READ/FUNCTION/RECURSIVE, plus create/insert/update on the
       results database for the CTAS. No ATTACH, PRAGMA, DDL/DML elsewhere.
    2. reads in the results database are limited to `allowed_tables` (the live tables)."""
    allowed = {t.lower() for t in allowed_tables or ()}

    def authorizer(action, arg1, arg2, dbname, source):
        if action in _ALLOWED_ACTIONS:
            if action == sqlite3.SQLITE_READ and dbname == "results":
                name = (arg1 or "").lower()
                if not name.startswith("sqlite_") and name not in allowed:
                    return sqlite3.SQLITE_DENY
            return sqlite3.SQLITE_OK
        if action in _RESULT_WRITES and dbname == "results":
            return sqlite3.SQLITE_OK
        return sqlite3.SQLITE_DENY

    return authorizer


def open_connection(
    telemetry_db: Path, results_db: Path, allowed_tables: set[str] | None = None
) -> sqlite3.Connection:
    """Read-only Telemetry Database connection with the results database attached and a
    restrictive authorizer installed."""
    con = sqlite3.connect(f"file:{telemetry_db}?mode=ro", uri=True)
    con.execute("ATTACH DATABASE ? AS results", (f"file:{results_db}?mode=rwc",))
    con.create_function("in_cidr", 2, in_cidr, deterministic=True)
    con.set_authorizer(_authorizer(allowed_tables))
    return con


_TIMEOUT_GUIDANCE = (
    "The query exceeded the {s:g} second time limit and was aborted. "
    "Aggregate the data (GROUP BY), narrow the etime time range, or add filters."
)
_TRUNCATED_NOTE = (
    "The Result Table was truncated to its first {n:,} rows; there are probably more. Do not present it as the "
    "complete answer. For a top-N question, use ORDER BY with a LIMIT; otherwise aggregate (GROUP BY) or add filters."
)


@contextmanager
def _deadline(con: sqlite3.Connection, timeout_s: float):
    """Abort statements on `con` after `timeout_s` seconds. Yields a callable that says whether
    the deadline fired."""
    end = time.monotonic() + timeout_s
    fired = False

    def _progress():
        nonlocal fired
        if time.monotonic() > end:
            fired = True
            return 1
        return 0

    con.set_progress_handler(_progress, 1000)
    try:
        yield lambda: fired
    finally:
        con.set_progress_handler(None, 0)


def _failure(e: sqlite3.Error, timed_out: bool, timeout_s: float, **extra) -> dict:
    """A failed-query result: a sanitised `error` for the LLM plus `raw_error` for the Trace."""
    error = _TIMEOUT_GUIDANCE.format(s=timeout_s) if timed_out else sanitize_error(str(e))
    return {"success": False, "error": error, "raw_error": str(e), **extra}


def run_query(
    telemetry_db: Path,
    results_db: Path,
    sql: str,
    timeout_s: float,
    max_rows: int,
    allowed_tables: set[str] | None = None,
    title: str = "",
) -> dict:
    """Run the SQL as CTAS into the results database. Returns Query Metadata only.

    At most `max_rows` rows are kept; a result of exactly `max_rows` rows is reported as truncated
    (`truncated_to`, `note`).

    On failure the dict has a sanitised `error` (safe for the LLM) and, for SQLite errors,
    a `raw_error` that the caller must keep for the Trace and strip before the LLM.
    The SELECT may reference only the Result Tables in `allowed_tables` (the Conversation's own).
    """
    try:
        select = validate_select(sql)
    except QueryRejected as e:
        return {"success": False, "error": str(e)}
    con = open_connection(telemetry_db, results_db, allowed_tables)
    executed = ""
    try:
        name = _free_name(con, title)
        with _deadline(con, timeout_s) as timed_out:
            for attempt in range(5):  # a concurrent query may take the name between check and create
                executed = f"CREATE TABLE results.{name} AS SELECT * FROM (\n{select}\n) LIMIT {int(max_rows)}"
                try:
                    con.execute(executed)
                    break
                except sqlite3.Error as e:
                    if attempt < 4 and f"table {name} already exists" in str(e):
                        name = _free_name(con, title)
                        continue
                    return _failure(e, timed_out(), timeout_s, executed_sql=executed)
        try:
            con.set_authorizer(None)
            con.commit()
            count = con.execute(f"SELECT COUNT(*) FROM results.{name}").fetchone()[0]
            cols = [
                {"name": r[1], "type": r[2] or "ANY"}
                for r in con.execute(f"PRAGMA results.table_info({name})")
            ]
        except sqlite3.Error as e:
            return _failure(e, False, timeout_s, executed_sql=executed)
    finally:
        con.close()
    result = {"success": True, "result_table": name, "columns": cols, "row_count": count, "executed_sql": executed}
    if count >= max_rows:
        # Exactly `max_rows` rows are indistinguishable from "more than max_rows" without fetching an
        # extra row, so we assume there are more.
        result |= {"truncated_to": max_rows, "note": _TRUNCATED_NOTE.format(n=max_rows)}
    return result


_COUNT_GUIDANCE = (
    "A count query must have the form SELECT COUNT(...) FROM ... (optionally COUNT(...) AS name) "
    "and return one non-negative integer, e.g. SELECT COUNT(*) FROM flows WHERE ..."
)
# The select list must be exactly one bare COUNT(...): no other column, alias trick or arithmetic.
_COUNT_FORM_RE = re.compile(r"^\s*select\s+count\s*\([^()]*\)\s*(?:as\s+\w+\s+)?from\s", re.I)


def run_count_query(
    telemetry_db: Path,
    results_db: Path,
    sql: str,
    timeout_s: float,
    allowed_tables: set[str] | None = None,
) -> dict:
    """Run a `SELECT COUNT(...) FROM ...` and return the number itself (no Result Table). The one
    place a telemetry-derived value reaches the LLM, so the select list must be a bare COUNT and
    the result one non-negative integer. Failures carry `raw_error` like `run_query`."""
    try:
        select = validate_select(sql)
    except QueryRejected as e:
        return {"success": False, "error": str(e)}
    if not _COUNT_FORM_RE.match(select):
        return {"success": False, "error": _COUNT_GUIDANCE}
    con = open_connection(telemetry_db, results_db, allowed_tables)
    try:
        with _deadline(con, timeout_s) as timed_out:
            try:
                rows = con.execute(f"SELECT * FROM (\n{select}\n) LIMIT 2").fetchall()
            except sqlite3.Error as e:
                return _failure(e, timed_out(), timeout_s)
    finally:
        con.close()
    if len(rows) != 1 or len(rows[0]) != 1:
        return {"success": False, "error": _COUNT_GUIDANCE}
    value = rows[0][0]
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return {"success": False, "error": _COUNT_GUIDANCE}
    return {"success": True, "count": value}


def result_table_columns(results_db: Path, name: str) -> list[str] | None:
    if not RESULT_TABLE_RE.match(name) or not Path(results_db).exists():
        return None
    con = sqlite3.connect(f"file:{results_db}?mode=ro", uri=True)
    try:
        cols = [r[1] for r in con.execute(f"PRAGMA table_info({name})")]
    finally:
        con.close()
    return cols or None


def describe_results_table(results_db: Path, name: str) -> dict | None:
    """Column names and types plus row count of a table in the results database, never rows.
    The caller has already checked that `name` is a registered, live table."""
    if not Path(results_db).exists():
        return None
    con = sqlite3.connect(f"file:{results_db}?mode=ro", uri=True)
    try:
        info = con.execute("SELECT name, type FROM pragma_table_info(?)", (name,)).fetchall()
        if not info:
            return None
        count = con.execute(f'SELECT COUNT(*) FROM "{name}"').fetchone()[0]
    finally:
        con.close()
    return {"columns": [{"name": n, "type": t or "ANY"} for n, t in info], "row_count": count}


class InvalidSort(ValueError):
    pass


def fetch_rows(
    results_db: Path,
    name: str,
    columns: list[str] | None = None,
    page: int = 1,
    page_size: int = 50,
    sort: str | None = None,
    descending: bool = False,
) -> dict:
    """Internal data path: one page of a Result Table as JSON-able data. Never an LLM tool.

    `sort` must exactly match a column of the Result Table (validated against its schema,
    then quoted); ties break on rowid so paging is stable.
    """
    all_cols = result_table_columns(results_db, name)
    if all_cols is None:
        raise KeyError(name)
    if sort is not None and sort not in all_cols:
        raise InvalidSort(sort)
    cols = [c for c in (columns or all_cols) if c in all_cols]
    select = ", ".join(_q(c) for c in cols)
    order = ""
    if sort is not None:
        order = f" ORDER BY {_q(sort)} {'DESC' if descending else 'ASC'}, rowid"
    con = sqlite3.connect(f"file:{results_db}?mode=ro", uri=True)
    try:
        total = con.execute(f"SELECT COUNT(*) FROM {name}").fetchone()[0]
        rows = [
            list(r)
            for r in con.execute(
                f"SELECT {select} FROM {name}{order or ' ORDER BY rowid'} LIMIT ? OFFSET ?",
                (page_size, (page - 1) * page_size),
            )
        ]
    finally:
        con.close()
    return {"columns": cols, "rows": rows, "total": total, "page": page, "page_size": page_size}


def result_table_row_count(results_db: Path, name: str) -> int | None:
    if result_table_columns(results_db, name) is None:
        return None
    con = sqlite3.connect(f"file:{results_db}?mode=ro", uri=True)
    try:
        return con.execute(f"SELECT COUNT(*) FROM {name}").fetchone()[0]
    finally:
        con.close()


def fetch_records(results_db: Path, name: str, limit: int) -> list[dict]:
    """Internal data path for charts: rows as JSON objects (Vega-Lite's shape), capped at
    `limit`. Never an LLM tool."""
    data = fetch_rows(results_db, name, page_size=limit)
    return [dict(zip(data["columns"], row)) for row in data["rows"]]


def iter_all_rows(results_db: Path, name: str):
    """Yield (columns, row iterator) for the whole Result Table (PSV export)."""
    all_cols = result_table_columns(results_db, name)
    if all_cols is None:
        raise KeyError(name)
    con = sqlite3.connect(f"file:{results_db}?mode=ro", uri=True)

    def gen():
        try:
            yield from con.execute(
                f"SELECT {', '.join(_q(c) for c in all_cols)} FROM {name} ORDER BY rowid"
            )
        finally:
            con.close()

    return all_cols, gen()


def _q(col: str) -> str:
    return '"' + col.replace('"', '""') + '"'
