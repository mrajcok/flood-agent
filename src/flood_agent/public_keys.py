"""A User's one Public Key: what an upload may be, and where it is kept (that User's app database)."""

import base64
import hashlib
import re
import sqlite3
from contextlib import closing
from dataclasses import astuple, dataclass
from pathlib import Path
from typing import Callable

# pyrage ships no type information.
from pyrage import RecipientError, ssh, x25519  # pyright: ignore[reportAttributeAccessIssue]

SCHEMA = """
CREATE TABLE IF NOT EXISTS public_key (
  id INTEGER PRIMARY KEY CHECK (id = 1),
  key_line TEXT NOT NULL,
  type TEXT NOT NULL,
  fingerprint TEXT NOT NULL,
  comment TEXT NOT NULL,
  added_at INTEGER NOT NULL
);
"""


MAX_LENGTH = 16_384  # characters: well over the longest accepted key line
ACCEPTED = ("one age public key (age1…), or an OpenSSH ssh-ed25519 or ssh-rsa (2048 to 4096 bits) public key "
            "line, with or without a comment")
# An age identity (its own or a plugin's), a PEM, OpenSSH, PGP or SSH2 private key block, or a PuTTY key file.
_PRIVATE_KEY = re.compile(r"AGE-SECRET-KEY-|AGE-PLUGIN-[A-Z0-9-]+-1|-{4,5} ?BEGIN [A-Z0-9 ]*PRIVATE KEY|PuTTY-User-Key-File",
                          re.IGNORECASE)
PRIVATE_KEY_WARNING = ("That is a private key, so it was refused and not stored. A private key should not be shared: "
                       "replace it with a new key pair, and give CANOPY only the public key.")


class KeyRefused(Exception):
    """An upload that is not an accepted Public Key. The message is shown to the User; it never repeats the upload."""


@dataclass(frozen=True)
class PublicKey:
    key_line: str  # the key as an age recipient or an OpenSSH public key line
    type: str  # "age", "ssh-ed25519" or "ssh-rsa"
    fingerprint: str
    comment: str  # "" when the key has none


def parse_public_key(text: str) -> PublicKey:
    """The one Public Key `text` holds; `KeyRefused` for anything else, a private key above all."""
    if _PRIVATE_KEY.search(text):
        raise KeyRefused(PRIVATE_KEY_WARNING)
    lines = [stripped for line in text.splitlines() if (stripped := line.strip())]
    if len(text) <= MAX_LENGTH and len(lines) == 1:
        try:
            return _age_key(lines[0]) if lines[0].startswith("age1") else _ssh_key(lines[0])
        except (ValueError, RecipientError):
            pass  # not a key: refused below
    raise KeyRefused(f"That is not one public key CANOPY accepts. Give {ACCEPTED}.")


def _age_key(recipient: str) -> PublicKey:
    x25519.Recipient.from_str(recipient)
    return PublicKey(recipient, "age", f"{recipient[:10]}…{recipient[-6:]}", "")


def _ssh_key(text: str) -> PublicKey:
    """ValueError or `RecipientError` unless `text` is an OpenSSH line that can be encrypted for."""
    kind, blob, *rest = text.split(maxsplit=2)
    comment = rest[0] if rest else ""
    line = f"{kind} {blob} {comment}".rstrip()
    ssh.Recipient.from_str(line)  # only ssh-ed25519, and ssh-rsa of 2048 to 4096 bits
    digest = hashlib.sha256(base64.b64decode(blob)).digest()
    return PublicKey(line, kind, "SHA256:" + base64.b64encode(digest).decode().rstrip("="), comment)


class PublicKeys:
    """Where one User's Public Key is kept: at most one, in their app database."""

    def __init__(self, app_db: Path, clock: Callable[[], int]):
        self.path, self._now = Path(app_db), clock
        with closing(sqlite3.connect(self.path)) as con, con:
            con.executescript(SCHEMA)

    def get(self) -> PublicKey | None:
        with closing(sqlite3.connect(self.path)) as con:
            row = con.execute("SELECT key_line, type, fingerprint, comment FROM public_key").fetchone()
        return None if row is None else PublicKey(*row)

    def save(self, key: PublicKey) -> None:
        """Keep `key`, in place of any key already on file."""
        with closing(sqlite3.connect(self.path)) as con, con:
            con.execute("INSERT OR REPLACE INTO public_key VALUES (1,?,?,?,?,?)", (*astuple(key), self._now()))

    def remove(self) -> None:
        with closing(sqlite3.connect(self.path)) as con, con:
            con.execute("DELETE FROM public_key")
