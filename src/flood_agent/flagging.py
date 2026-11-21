"""Hallucinated-value flagging: a server-side backstop for the "never state data values" rule.

The LLM never sees telemetry, so any IP address or large number in its reply that it was not
given is a probable invention. `find_unseen` scans reply text for two token kinds and returns
those absent from the allowed context (the text the LLM was actually sent).

Tokens
- IP: dotted IPv4, each octet 0-255, not glued to a word character or other dotted digits
  (so `v1.2.3.4` and `1.2.3.4.5` are ignored). A CIDR suffix is ignored; only the address counts.
- IPv6: full, `::`-compressed and IPv4-embedded forms that `ipaddress` accepts (so `12:30:45` is
  not one). A `/len` suffix is ignored. Compared in normal form (compressed, lower case), and an
  IPv4-mapped `::ffff:a.b.c.d` equals `a.b.c.d`.
- Number: digits optionally with thousands commas and a decimal part, optionally followed by a
  magnitude suffix (K/M/G/T glued to the number, with an optional unit such as B, bps, pps, MiB,
  or spaced from it only when a unit follows; or the words thousand/million/billion/trillion). Its value (suffix applied) must be >= LARGE_NUMBER. Numbers
  are compared by value, so `1,000,000`, `1000000` and `1M` are the same number.

- Domain: two or more dot-separated labels whose last label is in the bundled IANA TLD list
  (`tlds.txt`). A two-label token ending in a common file extension (`agent.py`) is skipped; three
  or more labels are always checked. Compared ignoring case and a trailing dot. A domain passes if
  it, or a name beneath it (so it is a parent of a seen name), appears in the allowed context; an
  unseen subdomain of a seen name is flagged. There is no allowlist of well-known domains.

Allowed context: every number and IP found anywhere in the texts passed in (no minimum size, no
word-boundary rules, so digits inside identifiers such as `rt_1700000000111` count too). Bare
numbers followed by a suffix are allowed both as written (`3`) and with the suffix applied (`3e9`).

Known gaps (by design, this is a heuristic): IPs with leading zeros (`010.001.002.003`), IPv6 with a zone (`fe80::1%eth0`),
IPs or numbers split across words/spaces/markup, numbers spelled out in words ("four million"),
values below LARGE_NUMBER (counts, ports, years), 4-part version strings with octets <= 255
(`1.2.3.4`) which are indistinguishable from IPs, arithmetic the LLM did itself (a sum or a
`window_end - 3600000` it states but never wrote in SQL), and values it copied from tool output
it was legitimately sent are never flagged (that is the point).
"""

import ipaddress
import re
from decimal import Decimal
from pathlib import Path

LARGE_NUMBER = Decimal(10_000)

_IP = re.compile(r"(?<![\w.])((?:\d{1,3}\.){3}\d{1,3})(?!\w|\.\d)")
# IPv6 candidates: runs of hex digits, colons and dots; only what ipaddress parses counts, so
# times (12:30:45) and ratios (3:1) fall out.
_IP6 = re.compile(r"(?<![\w:.])(?=[0-9A-Fa-f]*:)[0-9A-Fa-f:.]{2,}(?!\w)")
_TLDS = frozenset(
    line.strip().lower() for line in (Path(__file__).parent / "tlds.txt").read_text().splitlines()
    if line.strip() and not line.startswith("#")
)
_FILE_EXTS = frozenset(
    "py md sh rs js ts csv json sql txt html htm yaml yml toml xml png jpg jpeg gif svg pdf log zip gz tar "
    "c h cc cpp go rb pl php java css ini cfg conf lock pyc so exe bat ps1 tsv parquet".split()
)
_DOMAIN = re.compile(r"(?<![\w.-])((?:[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?\.)+[A-Za-z0-9-]+)\.?(?![\w-])")
_MAG = {"k": 10**3, "m": 10**6, "g": 10**9, "t": 10**12,
        "thousand": 10**3, "million": 10**6, "billion": 10**9, "trillion": 10**12}
_UNIT = r"(?:i?[Bb](?:ps|/s)?|pps)"
# A single-letter magnitude (K/M/G/T) must be glued to the number ("20M", "5Gbps"); with a space
# it needs a unit ("3 Gbps", "2 TB") so prose like "20 m long" or "top 10 T" is not read as a value.
_NUMBER = re.compile(
    r"(?P<int>\d{1,3}(?:,\d{3})+|\d+)(?P<frac>\.\d+)?"
    r"(?P<suffix>\s?(?P<word>thousand|million|billion|trillion)\b"
    r"|(?P<sp>\s)?(?P<letter>[KMGTkmgt])(?(sp)" + _UNIT + "|" + _UNIT + r"?)\b"
    r"|\s?(?:[Bb]ps|pps)\b)?"
)


def _numbers(text: str, strict: bool):
    """Yield (start, token, bare Decimal, suffixed Decimal) for each number in text.

    strict: reject numbers glued to word characters or inside dotted versions (reply scan)."""
    for m in _NUMBER.finditer(text):
        start, end = m.start(), m.end()
        if strict:
            if start and (text[start - 1].isalnum() or text[start - 1] in "_.,"):
                continue
            if end < len(text) and (text[end].isalnum() or text[end] == "_" or re.match(r"\.\d", text[end:end + 2])):
                continue
        bare = Decimal(m["int"].replace(",", "") + (m["frac"] or ""))
        key = (m["word"] or m["letter"] or "").lower()
        yield start, m.group(0).strip(), bare, bare * _MAG.get(key, 1)


def _valid_ip(s: str) -> bool:
    return all(int(o) <= 255 for o in s.split("."))


def _ipv6s(text: str):
    """Yield (start, end, token, normal form) for each real IPv6 address in text.

    The normal form is compressed lower case, or dotted IPv4 for an IPv4-mapped address."""
    for m in _IP6.finditer(text):
        token = m[0]
        while token:
            try:
                addr = ipaddress.IPv6Address(token)
            except ValueError:
                token = token[:-1] if token[-1] in ".:" else ""  # sentence-final punctuation
                continue
            yield m.start(), m.start() + len(token), token, str(addr.ipv4_mapped or addr.compressed)
            break


def _mask_ipv6(text: str) -> str:
    for start, end, _, _ in reversed(list(_ipv6s(text))):
        text = text[:start] + " " * (end - start) + text[end:]
    return text


def _domains(text: str, skip_files: bool):
    """Yield (start, token, lower-case name) for each domain name in text."""
    for m in _DOMAIN.finditer(text):
        labels = m[1].lower().split(".")
        if labels[-1] not in _TLDS or (skip_files and len(labels) == 2 and labels[-1] in _FILE_EXTS):
            continue
        yield m.start(), m[1], ".".join(labels)


def _allowed(texts: list[str]) -> tuple[set[str], set[Decimal], set[str]]:
    ips: set[str] = set()
    nums: set[Decimal] = set()
    domains: set[str] = set()  # every seen name and each of its parents
    for t in texts:
        for _, _, name in _domains(t, skip_files=False):
            labels = name.split(".")
            domains.update(".".join(labels[i:]) for i in range(len(labels) - 1))
        ips.update(norm for _, _, _, norm in _ipv6s(t))
        t = _mask_ipv6(t)
        ips.update(m[1] for m in _IP.finditer(t) if _valid_ip(m[1]))
        for _, _, bare, value in _numbers(t, strict=False):
            nums.update((bare, value))
    return ips, nums, domains


def find_unseen(text: str, allowed_texts: list[str]) -> list[tuple[str, str]]:
    """Return [(type, token)] ("ip", "number" or "domain") in order of first appearance, deduplicated."""
    allowed_ips, allowed_nums, allowed_domains = _allowed(allowed_texts)
    found: list[tuple[int, str, str]] = []
    forms: dict[str, str] = {}  # normal form -> token as first written
    for start, _, token, norm in _ipv6s(text):
        if norm not in allowed_ips and norm not in forms:
            forms[norm] = token
            found.append((start, "ip", token))
    text = _mask_ipv6(text)
    for m in _IP.finditer(text):
        if _valid_ip(m[1]) and m[1] not in allowed_ips:
            found.append((m.start(), "ip", m[1]))
    masked = _IP.sub(lambda m: " " * len(m[0]), text)  # an IP's octets are not separate numbers
    for start, token, _, value in _numbers(masked, strict=True):
        if value >= LARGE_NUMBER and value not in allowed_nums:
            found.append((start, "number", token))
    for start, token, name in _domains(text, skip_files=True):
        if name not in allowed_domains:
            found.append((start, "domain", token))
    seen, out = set(), []
    for _, kind, token in sorted(found):
        if token not in seen:
            seen.add(token)
            out.append((kind, token))
    return out


def find_unseen_values(text: str, allowed_texts: list[str]) -> list[str]:
    return [token for _, token in find_unseen(text, allowed_texts)]
