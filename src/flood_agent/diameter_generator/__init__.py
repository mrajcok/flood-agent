"""Synthetic Diameter Transaction Records for the `diameter` table.

Diameter has its own RNG (`seed + 5`), so `flows` and `dns_metadata` do not depend on it.

Each source of traffic is a function of the World that adds records to it, and attack entries to
`world.attacks`. Sources run in the order of SOURCES and share `world.rng`, so add a new one at the
end: the records of the sources before it stay as they were. An attack draws from its own RNG,
`world.fork("<name>")`.

- `network`: the Networks, their Nodes and the Subscribers
- `records`: the Record (one row) and the World, which records each request and its answer
- `s6a`: the baseline MME-HSS traffic
- `clr_spoof`: the spoofed Cancel-Location DoS
"""

import sqlite3

from flood_agent.diameter_generator import clr_spoof, s6a
from flood_agent.diameter_generator.records import COLUMNS, INDEXES, SCHEMA, World

__all__ = ["INDEXES", "SCHEMA", "generate_diameter"]

SOURCES = (
    s6a.baseline,
    clr_spoof.attack,
)


def generate_diameter(con: sqlite3.Connection, seed: int, scale: float) -> list[dict]:
    """Fill the `diameter` table; return the injected attack records."""
    world = World.build(seed, scale)
    for source in SOURCES:
        source(world)
    world.records.sort(key=lambda r: (r.etime, r.stime))  # etime order (Time Key stand-in)
    con.executemany(
        f"INSERT INTO diameter ({', '.join(COLUMNS)}) VALUES ({', '.join('?' * len(COLUMNS))})",
        ([getattr(r, c) for c in COLUMNS] for r in world.records),
    )
    return world.attacks
