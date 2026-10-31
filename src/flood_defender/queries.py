"""Query execution against the Telemetry Database and Result Table access.

Telemetry Database: opened read-only. Result Tables: created via CTAS in a separate,
writable results database that is ATTACHed to the (read-only) telemetry connection.
"""

import ipaddress
import re
import sqlite3
import uuid
from pathlib import Path

RESULT_TABLE_RE = re.compile(r"^rt_[0-9a-f]{8}$")
_SELECT_RE = re.compile(r"^\s*(select|with)\b", re.IGNORECASE)


class QueryRejected(Exception):
    pass


def validate_select(sql: str) -> str:
    """Basic single read-only SELECT guard. Returns the SQL without trailing semicolons."""
    cleaned = sql.strip().rstrip(";").strip()
    if not cleaned or not _SELECT_RE.match(cleaned):
        raise QueryRejected("Only a single SELECT statement is allowed.")
    if ";" in cleaned:
        raise QueryRejected("Only a single SELECT statement is allowed.")
    return cleaned


def in_cidr(ip, cidr):
    """SQLite UDF: 1 if IPv4 `ip` is inside `cidr`, else 0 (NULL/invalid input -> NULL).
    Needs an Ocient mapping when the Telemetry Database moves there."""
    if ip is None or cidr is None:
        return None
    try:
        return int(ipaddress.IPv4Address(ip) in ipaddress.IPv4Network(cidr, strict=False))
    except ValueError:
        return None


def sanitize_error(message: str) -> str:
    """Strip quoted literals and numbers from an SQLite error before it reaches the LLM."""
    message = re.sub(r"'[^']*'|\"[^\"]*\"", "<literal>", message)
    return re.sub(r"\d+(\.\d+)?", "<n>", message)


def run_query(telemetry_db: Path, results_db: Path, sql: str) -> dict:
    """Run the SQL as CTAS into the results database. Returns Query Metadata only."""
    try:
        select = validate_select(sql)
    except QueryRejected as e:
        return {"success": False, "error": str(e)}
    name = f"rt_{uuid.uuid4().hex[:8]}"
    con = sqlite3.connect(f"file:{telemetry_db}?mode=ro", uri=True)
    con.create_function("in_cidr", 2, in_cidr, deterministic=True)
    try:
        con.execute("ATTACH DATABASE ? AS results", (f"file:{results_db}?mode=rwc",))
        con.execute(f"CREATE TABLE results.{name} AS {select}")
        con.commit()
        cols = [
            {"name": r[1], "type": r[2] or "ANY"}
            for r in con.execute(f"PRAGMA results.table_info({name})")
        ]
        count = con.execute(f"SELECT COUNT(*) FROM results.{name}").fetchone()[0]
    except sqlite3.Error as e:
        return {"success": False, "error": sanitize_error(str(e))}
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


def iter_all_rows(results_db: Path, name: str):
    """Yield (columns, row iterator) for the whole Result Table (CSV export)."""
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
