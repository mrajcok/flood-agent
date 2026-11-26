"""Downloading a Result Table or Saved Table as a PSV file: which modes it may be had in, and the file's content.

A restricted table cannot be had plain (ADR 0008). In a masked file each value of a Restricted Column is a token: the
same value gives the same token anywhere in that file, under a secret made for that one download and never kept. An
encrypted file is the plain one, encrypted with age for a User's Public Key.
"""

import csv
import hmac
import io
import secrets
import tempfile
from dataclasses import dataclass
from typing import IO, Collection, Iterable, Iterator, Literal, Sequence

# pyrage ships no type information.
from pyrage import encrypt_io  # pyright: ignore[reportAttributeAccessIssue]

from flood_agent.catalog import Table
from flood_agent.public_keys import PublicKey

Mode = Literal["plain", "masked", "encrypted"]
_CHUNK = 65536  # characters buffered before a piece of the file is sent; bytes per piece of an encrypted one
_SPOOL = 1 << 20  # bytes of an encrypted file held in memory before it goes to a temporary file


class DownloadRefused(Exception):
    """The table cannot be downloaded in the mode asked for. The message is shown to the User."""


class NoPublicKey(Exception):
    """An encrypted download was asked for with no Public Key to encrypt it for. The message is shown to the User."""


@dataclass(frozen=True)
class Download:
    """A file to send: its name and media type, the table as the download treats it, and its content in pieces (a
    PSV file's text is made as it is read)."""

    filename: str
    table: Table
    chunks: Iterator[str] | Iterator[bytes]
    media_type: str = "text/plain; charset=utf-8"
    key: PublicKey | None = None  # the one an encrypted file is for


def check(table: Table, mode: Mode, key: PublicKey | None = None) -> None:
    """`DownloadRefused` unless `table` may be downloaded in `mode`; `NoPublicKey` for an encrypted one without the
    User's `key`."""
    if mode == "encrypted" and key is None:
        raise NoPublicKey("No public key on file")
    if not table.restricted or mode == "encrypted" or (mode == "masked" and not table.lineage_unknown):
        return
    if table.lineage_unknown:
        raise DownloadRefused(f"CANOPY couldn't tell which columns of {table.name} carry restricted values, "
                              "so it can only be downloaded encrypted.")
    raise DownloadRefused(f"{table.name} has Restricted Columns ({', '.join(table.restricted_columns)}), "
                          "so it cannot be downloaded plain. Download it masked or encrypted.")


def psv_file(table: Table, mode: Mode, columns: Sequence[str], rows: Iterable[tuple],
             key: PublicKey | None = None) -> Download:
    """The download of `table`'s `rows` in `mode`, which `check` has allowed; an encrypted one is for `key`, and all
    the rows have been read and encrypted when this returns."""
    if mode == "encrypted":
        assert key is not None
        return Download(f"{table.name}.psv.age", table, _encrypted(_psv(columns, rows), key),
                        "application/octet-stream", key)
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


def _encrypted(text: Iterable[str], key: PublicKey) -> Iterator[bytes]:
    """`text`, as UTF-8, in age's binary format for `key`. The ciphertext is spooled, so a large one waits in a
    temporary file, not in memory; the plaintext is never written anywhere."""
    spool = tempfile.SpooledTemporaryFile(max_size=_SPOOL)
    try:
        encrypt_io(_Reader(text), spool, [key.recipient()])
        spool.seek(0)
    except BaseException:
        spool.close()
        raise
    return _pieces(spool)


def _pieces(file: IO[bytes]) -> Iterator[bytes]:
    with file:
        while piece := file.read(_CHUNK):
            yield piece


class _Reader:
    """`text` as the binary stream `encrypt_io` reads, taking a piece of it only when the last is used up."""

    def __init__(self, text: Iterable[str]):
        self._text, self._left = iter(text), memoryview(b"")

    def read(self, size: int) -> bytes:
        while not self._left:
            piece = next(self._text, None)
            if piece is None:
                return b""
            self._left = memoryview(piece.encode())
        out, self._left = self._left[:size], self._left[size:]
        return bytes(out)


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
