"""The configured Users, their password hashes and where each one's databases are, and the command that makes a USERS entry:

    uv run flood-agent-user NAME

A hash is `scrypt$n$r$p$salt$digest` (salt and digest in base64), so it carries what is needed to check a password
against it. It holds neither `,` nor `:`, which separate the entries of USERS.
"""

import argparse
import base64
import getpass
import hashlib
import hmac
import re
import secrets
import sys
from dataclasses import dataclass
from pathlib import Path

USER_NAME = re.compile(r"[a-z][a-z0-9_]{0,30}")

# One of OWASP's recommended scrypt settings: a fraction of a second and 32 MiB per guess, so that guesses arriving
# together cannot take much memory.
_N, _R, _P = 2**15, 8, 3
_MAX_MEMORY = 2**30  # a configured hash may not ask for more than this to check
_MAX_P = 16


@dataclass(frozen=True)
class UserDatabases:
    """One User's own app database and results database, which no other User's parts open."""

    app_db: Path
    results_db: Path


def user_databases(users_dir: Path, name: str) -> UserDatabases:
    """User `name`'s databases under the per-User data directory."""
    return UserDatabases(users_dir / name / "app.db", users_dir / name / "results.db")


@dataclass(frozen=True)
class PasswordHash:
    n: int
    r: int
    p: int
    salt: bytes
    digest: bytes

    @classmethod
    def parse(cls, text: str) -> "PasswordHash":
        """ValueError unless `text` is a hash that `verify` can check."""
        scheme, n_text, r_text, p_text, salt_text, digest_text = text.split("$")
        n, r, p = int(n_text), int(r_text), int(p_text)
        salt, digest = (base64.b64decode(t, validate=True) for t in (salt_text, digest_text))
        # scrypt's own limits (n a power of two below 2**(16 r)), so a bad hash fails here and not at sign-in.
        usable = (scheme == "scrypt" and r >= 1 and 1 <= p <= _MAX_P and n >= 2 and n & (n - 1) == 0
                  and n.bit_length() <= 16 * r and 128 * r * n <= _MAX_MEMORY and salt and len(digest) >= 16)
        if not usable:
            raise ValueError("not a usable scrypt hash")
        return cls(n, r, p, salt, digest)

    def __str__(self) -> str:
        salt, digest = (base64.b64encode(b).decode() for b in (self.salt, self.digest))
        return f"scrypt${self.n}${self.r}${self.p}${salt}${digest}"

    def verify(self, password: str) -> bool:
        """Whether this is the hash of `password`, compared in constant time."""
        return hmac.compare_digest(_scrypt(password, self.salt, self.n, self.r, self.p, len(self.digest)), self.digest)

    def decoy(self) -> "PasswordHash":
        """A hash no password matches that takes as long to check as this one."""
        return PasswordHash(self.n, self.r, self.p, secrets.token_bytes(len(self.salt)),
                            secrets.token_bytes(len(self.digest)))


def _scrypt(password: str, salt: bytes, n: int, r: int, p: int, length: int) -> bytes:
    memory = 128 * r * (n + p + 2) + 1024  # what OpenSSL needs; its own default limit is too low
    return hashlib.scrypt(password.encode(), salt=salt, n=n, r=r, p=p, maxmem=memory,
                          dklen=length)


def hash_password(password: str, n: int = _N, r: int = _R, p: int = _P) -> PasswordHash:
    salt = secrets.token_bytes(16)
    return PasswordHash(n, r, p, salt, _scrypt(password, salt, n, r, p, 32))


def _check_name(name: str) -> None:
    if not USER_NAME.fullmatch(name):
        raise ValueError(f"invalid User name {name!r}: it must match {USER_NAME.pattern}")


def parse_users(text: str) -> dict[str, PasswordHash]:
    """The Users in a `name:hash,name:hash` list. ValueError names the problem; it never repeats a hash, or an
    entry with no `name:` (which could be a password)."""
    users: dict[str, PasswordHash] = {}
    entries = [e.strip() for e in text.split(",") if e.strip()]
    if not entries:
        raise ValueError("USERS is empty")
    for number, entry in enumerate(entries, 1):
        name, colon, hashed = entry.partition(":")
        if not colon:
            raise ValueError(f"USERS entry {number} is not name:hash")
        _check_name(name)
        if name in users:
            raise ValueError(f"User name {name!r} appears twice in USERS")
        try:
            users[name] = PasswordHash.parse(hashed)
        except ValueError:
            raise ValueError(f"malformed password hash for User {name!r}: make one with flood-agent-user") from None
    return users


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="flood-agent-user", description="Print a USERS entry for .env.")
    parser.add_argument("name", help=f"the User name ({USER_NAME.pattern})")
    name = parser.parse_args(argv).name
    try:
        _check_name(name)
        password = getpass.getpass("Password: ")
        if not password:
            raise ValueError("the password is empty")
        if getpass.getpass("Password again: ") != password:
            raise ValueError("the passwords do not match")
    except ValueError as e:
        print(e, file=sys.stderr)
        return 1
    print(f"{name}:{hash_password(password)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
