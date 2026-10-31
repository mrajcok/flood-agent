"""Hallucinated-value flagging: a server-side backstop for the "never state data values" rule.

The LLM never sees telemetry, so any IP address or large number in its reply that it was not
given is a probable invention. `find_unseen` scans reply text for two token kinds and returns
those absent from the allowed context (the text the LLM was actually sent).

Tokens
- IP: dotted IPv4, each octet 0-255, not glued to a word character or other dotted digits
  (so `v1.2.3.4` and `1.2.3.4.5` are ignored). A CIDR suffix is ignored; only the address counts.
- Number: digits optionally with thousands commas and a decimal part, optionally followed by a
  magnitude suffix (K/M/G/T glued to the number, with an optional unit such as B, bps, pps, MiB,
  or spaced from it only when a unit follows; or the words thousand/million/billion/trillion). Its value (suffix applied) must be >= LARGE_NUMBER. Numbers
  are compared by value, so `1,000,000`, `1000000` and `1M` are the same number.

Allowed context: every number and IP found anywhere in the texts passed in (no minimum size, no
word-boundary rules, so digits inside identifiers such as `rt_1700000000111` count too). Bare
numbers followed by a suffix are allowed both as written (`3`) and with the suffix applied (`3e9`).

Known gaps (by design, this is a heuristic): IPs with leading zeros (`010.001.002.003`), IPv6,
IPs or numbers split across words/spaces/markup, numbers spelled out in words ("four million"),
values below LARGE_NUMBER (counts, ports, years), 4-part version strings with octets <= 255
(`1.2.3.4`) which are indistinguishable from IPs, arithmetic the LLM did itself (a sum or a
`window_end - 3600000` it states but never wrote in SQL), and values it copied from tool output
it was legitimately sent are never flagged (that is the point).
"""

import re
from decimal import Decimal

LARGE_NUMBER = Decimal(10_000)

_IP = re.compile(r"(?<![\w.])((?:\d{1,3}\.){3}\d{1,3})(?!\w|\.\d)")
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


def _allowed(texts: list[str]) -> tuple[set[str], set[Decimal]]:
    ips: set[str] = set()
    nums: set[Decimal] = set()
    for t in texts:
        ips.update(m[1] for m in _IP.finditer(t) if _valid_ip(m[1]))
        for _, _, bare, value in _numbers(t, strict=False):
            nums.update((bare, value))
    return ips, nums


def find_unseen(text: str, allowed_texts: list[str]) -> list[tuple[str, str]]:
    """Return [(type, token)] ("ip" or "number") in order of first appearance, deduplicated."""
    allowed_ips, allowed_nums = _allowed(allowed_texts)
    found: list[tuple[int, str, str]] = []
    for m in _IP.finditer(text):
        if _valid_ip(m[1]) and m[1] not in allowed_ips:
            found.append((m.start(), "ip", m[1]))
    masked = _IP.sub(lambda m: " " * len(m[0]), text)  # an IP's octets are not separate numbers
    for start, token, _, value in _numbers(masked, strict=True):
        if value >= LARGE_NUMBER and value not in allowed_nums:
            found.append((start, "number", token))
    seen, out = set(), []
    for _, kind, token in sorted(found):
        if token not in seen:
            seen.add(token)
            out.append((kind, token))
    return out


def find_unseen_values(text: str, allowed_texts: list[str]) -> list[str]:
    return [token for _, token in find_unseen(text, allowed_texts)]
