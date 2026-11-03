"""Lookup Tables: user-pasted rows turned into a table in the results database (ADR 0003).

Created by the web app over plain HTTP, never by the LLM. Registration in the app database's
`lookup_tables` registry, not the `lookup_` name prefix, is what makes a table a Lookup Table.
"""

import csv
import io
import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path

PREFIX = "lookup_"
_SUFFIX_RE = re.compile(r"^[A-Za-z0-9_]{1,60}$")


class InvalidLookupTable(ValueError):
    """The paste or name cannot make a Lookup Table; the message is for the user."""


@dataclass(frozen=True)
class ParsedRows:
    columns: list[str]
    rows: list[list[str | None]]


def table_name(suffix: str) -> str:
    """`lookup_<suffix>`, lower-cased. The name goes into SQL, so only [A-Za-z0-9_] is allowed."""
    suffix = suffix.strip()
    if not _SUFFIX_RE.match(suffix):
        raise InvalidLookupTable("A name must be 1 to 60 letters, digits or underscores.")
    return PREFIX + suffix.lower()


def parse_rows(text: str) -> ParsedRows:
    """Header line first. Separator: tab if the header line has one, else `|`, else comma;
    CSV-style quoting. Cells are trimmed, blank lines skipped, empty cells NULL."""
    lines = [line for line in text.splitlines() if line.strip()]
    if not lines:
        raise InvalidLookupTable("Paste the column names and at least one row.")
    header = lines[0]
    sep = "\t" if "\t" in header else "|" if "|" in header else ","
    records = [[c.strip() for c in rec] for rec in csv.reader(io.StringIO(text.lstrip("\r\n")), delimiter=sep)]
    records = [rec for rec in records if any(rec)]
    columns, body = records[0], records[1:]
    if not body:
        raise InvalidLookupTable("Paste at least one row after the column names.")
    if not all(columns):
        raise InvalidLookupTable("Every column needs a name in the first row.")
    if len({c.lower() for c in columns}) != len(columns):
        raise InvalidLookupTable("Column names must be different from each other.")
    rows: list[list[str | None]] = []
    for rec in body:
        if len(rec) > len(columns):
            raise InvalidLookupTable("A row has more cells than there are columns.")
        rows.append([c or None for c in rec] + [None] * (len(columns) - len(rec)))
    return ParsedRows(columns, rows)


def _q(identifier: str) -> str:
    return '"' + identifier.replace('"', '""') + '"'


def create_table(results_db: Path, name: str, parsed: ParsedRows) -> None:
    """Create and fill `name` (already validated by `table_name`) with all-TEXT columns."""
    Path(results_db).parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(results_db)
    try:
        with con:
            if con.execute("SELECT 1 FROM sqlite_master WHERE name=? COLLATE NOCASE", (name,)).fetchone():
                raise InvalidLookupTable("A table with that name already exists.")
            cols = ", ".join(f"{_q(c)} TEXT" for c in parsed.columns)
            con.execute(f"CREATE TABLE {_q(name)} ({cols})")
            marks = ", ".join("?" * len(parsed.columns))
            con.executemany(f"INSERT INTO {_q(name)} VALUES ({marks})", parsed.rows)
    finally:
        con.close()
