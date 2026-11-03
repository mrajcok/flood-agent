"""The rules for table names in the results database, in one place.

A name is a plain SQL identifier (letters, digits, underscores, starting with a letter; valid
unquoted in SQLite and Ocient). `rt_` names are generated for Result Tables and `lookup_` names
belong to Lookup Tables, so a user-chosen Result Table name may not start with either.
"""

import re

MAX_LEN = 60
RULE = f"use letters, digits and underscores, starting with a letter, at most {MAX_LEN} characters"
RESERVED_PREFIXES = ("rt_", "lookup_")
_IDENTIFIER_RE = re.compile(rf"[A-Za-z][A-Za-z0-9_]{{0,{MAX_LEN - 1}}}")
# The name a Result Table is given when it is made: rt_<YYYYMMDD>_<HHMMSS>[_<n>].
GENERATED_RE = re.compile(r"rt_\d{8}_\d{6}(?:_\d+)?")


def is_identifier(value: str) -> bool:
    return _IDENTIFIER_RE.fullmatch(value) is not None


def is_reserved(name: str) -> bool:
    return name.lower().startswith(RESERVED_PREFIXES)


def quote(identifier: str) -> str:
    """`identifier` as a quoted SQL identifier."""
    return '"' + identifier.replace('"', '""') + '"'
