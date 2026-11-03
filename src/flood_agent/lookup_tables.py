"""Lookup Tables: user-pasted rows turned into a table in the results database (ADR 0003).

Created by the web app over plain HTTP, never by the LLM. Registration in the app database's
`lookup_tables` registry, not the `lookup_` name prefix, is what makes a table a Lookup Table.
"""

import csv
import io
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from flood_agent import table_names

PREFIX = "lookup_"
MAX_ROWS = 100_000
FEW = 3  # how many offenders a message lists


class InvalidLookupTable(ValueError):
    """The paste or name cannot make a Lookup Table; the message is for the user."""


class TableExists(InvalidLookupTable):
    """`write_table` found the name taken and was not asked to replace it."""


@dataclass(frozen=True)
class ParsedRows:
    columns: list[str]
    rows: list[list[str | None]]


def _check_identifier(value: str, what: str) -> None:
    if not table_names.is_identifier(value):
        raise InvalidLookupTable(f'{what} "{value}" is not allowed: {table_names.RULE}.')


def table_name(suffix: str) -> str:
    """`lookup_<suffix>`, lower-cased. The name goes into SQL, so only [A-Za-z0-9_] is allowed."""
    suffix = suffix.strip()
    _check_identifier(suffix, "The table name")
    return PREFIX + suffix.lower()


def _few(items: list[str]) -> str:
    more = f" and {len(items) - FEW} more" if len(items) > FEW else ""
    return ", ".join(items[:FEW]) + more


def parse_rows(text: str) -> ParsedRows:
    """Header line first. Separator: tab if the header line has one, else `|`, else comma;
    CSV-style quoting. Cells are trimmed, blank lines skipped, empty cells NULL.
    Line numbers in messages count from 1 at the first line of the paste."""
    lines = [line for line in text.splitlines() if line.strip()]
    if not lines:
        raise InvalidLookupTable("Paste the column names and at least one row.")
    header = lines[0]
    sep = "\t" if "\t" in header else "|" if "|" in header else ","
    reader = csv.reader(io.StringIO(text), delimiter=sep)
    records: list[tuple[int, list[str]]] = []
    for rec in reader:
        cells = [c.strip() for c in rec]
        if any(cells):
            records.append((reader.line_num, cells))
    if not records:  # only separators
        raise InvalidLookupTable("Paste the column names and at least one row.")
    columns, body = records[0][1], records[1:]
    if not all(columns):
        raise InvalidLookupTable("Every column needs a name in the first row.")
    for c in columns:
        _check_identifier(c, "The column name")
    if len({c.lower() for c in columns}) != len(columns):
        repeated = sorted({c for c in columns if [x.lower() for x in columns].count(c.lower()) > 1})
        raise InvalidLookupTable(f"Column names must be different from each other, ignoring case: {_few(repeated)}.")
    if not body:
        raise InvalidLookupTable("Paste at least one row after the column names.")
    if len(body) > MAX_ROWS:
        raise InvalidLookupTable(f"At most {MAX_ROWS:,} rows are allowed; the paste has {len(body):,}.")
    bad = [str(n) for n, rec in body if len(rec) != len(columns)]
    if bad:
        raise InvalidLookupTable(
            f"Every row needs {len(columns)} cells, like the column names. Check line {_few(bad)}."
        )
    seen: dict[str, list[str]] = {}
    for n, rec in body:
        if rec[0]:
            seen.setdefault(rec[0], []).append(str(n))
    dupes = [f"{v} (lines {', '.join(ns)})" for v, ns in seen.items() if len(ns) > 1]
    if dupes:
        raise InvalidLookupTable(f"Values in the first column must be different. Repeated: {_few(dupes)}.")
    return ParsedRows(columns, [[c or None for c in rec] for _, rec in body])


def write_table(results_db: Path, name: str, parsed: ParsedRows, replace: bool) -> None:
    """Create and fill `name` (already validated by `table_name`) with all-TEXT columns.

    A name already used raises `TableExists` unless `replace`, in which case the old table is dropped.
    Everything happens in one explicit transaction (sqlite3's default handling would auto-commit the
    DROP and CREATE), so a failure at any point leaves the old table and its rows as they were."""
    Path(results_db).parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(results_db, isolation_level=None)
    try:
        con.execute("BEGIN IMMEDIATE")
        try:
            if con.execute("SELECT 1 FROM sqlite_master WHERE name=? COLLATE NOCASE", (name,)).fetchone():
                if not replace:
                    raise TableExists(f"A table named {name} already exists.")
                con.execute(f"DROP TABLE {table_names.quote(name)}")
            cols = ", ".join(f"{table_names.quote(c)} TEXT" for c in parsed.columns)
            con.execute(f"CREATE TABLE {table_names.quote(name)} ({cols})")
            marks = ", ".join("?" * len(parsed.columns))
            con.executemany(f"INSERT INTO {table_names.quote(name)} VALUES ({marks})", parsed.rows)
            con.execute("COMMIT")
        except BaseException:
            con.execute("ROLLBACK")
            raise
    finally:
        con.close()
