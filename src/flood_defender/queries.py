"""Query execution against the Telemetry Database and Result Table access.

Telemetry Database: opened read-only. Result Tables: created via CTAS in a separate,
writable results database that is ATTACHed to the (read-only) telemetry connection.
"""

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
