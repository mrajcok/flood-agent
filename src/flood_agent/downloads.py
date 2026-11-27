"""Downloading a Result Table or Saved Table as a PSV file: which modes it may be had in, and the file's content.

A Restricted Table cannot be had plain (ADR 0008). An encrypted file is the plain one, encrypted with age for a
User's Public Key.
"""

import csv
import io
import tempfile
from dataclasses import dataclass
from typing import IO, Iterable, Iterator, Literal, Sequence

# pyrage ships no type information.
from pyrage import encrypt_io  # pyright: ignore[reportAttributeAccessIssue]

from flood_agent.catalog import Restriction, Table
from flood_agent.public_keys import PublicKey

Mode = Literal["plain", "encrypted"]
_CHUNK = 65536  # characters buffered before a piece of the file is sent; bytes per piece of an encrypted one
_SPOOL = 1 << 20  # bytes of an encrypted file held in memory before it goes to a temporary file


class DownloadRefused(Exception):
    """The table cannot be downloaded in the mode asked for. The message is shown to the User."""


class NoPublicKey(Exception):
    """An encrypted download was asked for with no Public Key to encrypt it for. The message is shown to the User."""


@dataclass(frozen=True)
class Allowed:
    """A download `check` has allowed, of `table` under `restriction`. `key` is the one an encrypted file is for,
    and None for a plain one."""

    table: Table
    restriction: Restriction
    key: PublicKey | None = None


@dataclass(frozen=True)
class Download:
    """A file to send: what was allowed, its name and media type, and its content in pieces (a PSV file's text is
    made as it is read)."""

    allowed: Allowed
    filename: str
    chunks: Iterator[str] | Iterator[bytes]
    media_type: str = "text/plain; charset=utf-8"


def check(table: Table, restriction: Restriction, mode: Mode, key: PublicKey | None = None) -> Allowed:
    """The download of `table`, restricted as `restriction` says, in `mode`; `DownloadRefused` if it may not be had
    that way, `NoPublicKey` for an encrypted one without the User's `key`."""
    if mode == "encrypted":
        if key is None:
            raise NoPublicKey("No Public Key on file")
        return Allowed(table, restriction, key)
    if mode == "plain" and not restriction.restricted:
        return Allowed(table, restriction)
    raise DownloadRefused(_why(table.name, restriction))


def _why(name: str, restriction: Restriction) -> str:
    """Why the Restricted Table `name` cannot be had plain."""
    reasons = [f"it has data from {table}, which holds Restricted Columns ({', '.join(columns)})"
               for table, columns in restriction.restricting_sources]
    if restriction.unknown_origin:
        reasons.append("CANOPY doesn't know where its data came from")
    return f"{name} is a Restricted Table ({'; '.join(reasons)}), so it can only be downloaded encrypted."


def psv_file(allowed: Allowed, columns: Sequence[str], rows: Iterable[tuple]) -> Download:
    """The file of the `allowed` download, holding `rows`. All the rows of an encrypted one have been read and
    encrypted when this returns."""
    name = allowed.table.name
    if allowed.key is not None:
        return Download(allowed, f"{name}.psv.age", _encrypted(_psv(columns, rows), allowed.key),
                        "application/octet-stream")
    return Download(allowed, f"{name}.psv", _psv(columns, rows))


def _psv(columns: Sequence[str], rows: Iterable[tuple]) -> Iterator[str]:
    """The header line and `rows` as pipe-separated values."""
    buf = io.StringIO()
    w = csv.writer(buf, delimiter="|", lineterminator="\r\n")
    w.writerow([_psv_safe(c) for c in columns])
    for row in rows:
        w.writerow([_psv_safe(v) for v in row])
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


def _psv_safe(v):
    """Neutralise spreadsheet formula injection in text cells; numbers pass through."""
    if v is None:
        return ""
    if isinstance(v, str) and v[:1] in ("=", "+", "-", "@", "\t", "\r"):
        return "'" + v
    return v
