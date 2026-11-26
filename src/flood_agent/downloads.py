"""Downloading a Result Table or Saved Table as a PSV file: which modes it may be had in, and the file's text.

A restricted table cannot be had plain (ADR 0008). In a masked file each value of a Restricted Column is a token: the
same value gives the same token anywhere in that file, under a secret made for that one download and never kept.
"""

import csv
import hmac
import io
import secrets
from dataclasses import dataclass
from typing import Collection, Iterable, Iterator, Literal, Sequence

from flood_agent.catalog import Table

Mode = Literal["plain", "masked"]
_CHUNK = 65536  # characters buffered before a piece of the file is sent


class DownloadRefused(Exception):
    """The table cannot be downloaded in the mode asked for. The message is shown to the User."""


@dataclass(frozen=True)
class Download:
    """A file to send: its name, the table as the download treats it, and its text in pieces, made as it is read."""

    filename: str
    table: Table
    chunks: Iterator[str]


def check(table: Table, mode: Mode) -> None:
    """`DownloadRefused` unless `table` may be downloaded in `mode`."""
    if not table.restricted or (mode == "masked" and not table.lineage_unknown):
        return
    if table.lineage_unknown:
        raise DownloadRefused(f"CANOPY couldn't tell which columns of {table.name} carry restricted values, "
                              "so it can only be downloaded encrypted.")
    raise DownloadRefused(f"{table.name} has Restricted Columns ({', '.join(table.restricted_columns)}), "
                          "so it cannot be downloaded plain. Download it masked or encrypted.")


def psv_file(table: Table, mode: Mode, columns: Sequence[str], rows: Iterable[tuple]) -> Download:
    """The download of `table`'s `rows` in `mode`, which `check` has allowed."""
    if mode == "masked":
        return Download(f"{table.name}.masked.psv", table, _psv(columns, rows, table.restricted_columns))
    return Download(f"{table.name}.psv", table, _psv(columns, rows))


def _psv(columns: Sequence[str], rows: Iterable[tuple], masked: Collection[str] = ()) -> Iterator[str]:
    """The header line and `rows` as pipe-separated values, the values of the `masked` columns as tokens."""
    secret = secrets.token_bytes(32)  # shared by every masked column of this file; gone when it has been made
    tokenise = [c in masked for c in columns]
    buf = io.StringIO()
    w = csv.writer(buf, delimiter="|", lineterminator="\r\n")
    w.writerow([_psv_safe(c) for c in columns])
    for row in rows:
        values = [_psv_safe(v) for v in row]
        if masked:
            values = [_token(secret, v) if hide else v for v, hide in zip(values, tokenise)]
        w.writerow(values)
        if buf.tell() > _CHUNK:
            yield buf.getvalue()
            buf.seek(0)
            buf.truncate()
    yield buf.getvalue()


def _token(secret: bytes, value: object) -> str:
    """The token that stands for `value`, a field of a plain file; an empty field (NULL included) stays empty."""
    text = str(value)  # what `csv` writes
    return hmac.digest(secret, text.encode(), "sha256")[:8].hex() if text else ""


def _psv_safe(v):
    """Neutralise spreadsheet formula injection in text cells; numbers pass through."""
    if v is None:
        return ""
    if isinstance(v, str) and v[:1] in ("=", "+", "-", "@", "\t", "\r"):
        return "'" + v
    return v
