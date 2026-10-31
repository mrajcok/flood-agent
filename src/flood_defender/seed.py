"""Create a tiny seeded demo Telemetry Database (the real generator is a later ticket).

    python -m flood_defender.seed data/telemetry.db
"""

import random
import sqlite3
import sys
from pathlib import Path

SCHEMA = """
CREATE TABLE flows (
  sip TEXT, dip TEXT, sport INTEGER, dport INTEGER, proto INTEGER,
  tcp_flags INTEGER, circuit TEXT, stime INTEGER, etime INTEGER,
  bytes INTEGER, pkts INTEGER
);
CREATE INDEX flows_etime ON flows(etime);
CREATE INDEX flows_sip ON flows(sip);
CREATE INDEX flows_dip ON flows(dip);
"""

WINDOW_START = 1767225600000  # keep in sync with semantic_layer.yaml


def seed(path: Path, n: int = 500, seed_value: int = 1) -> None:
    rng = random.Random(seed_value)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.unlink(missing_ok=True)
    circuits = ["".join(rng.choices("ABCDEF0123456789", k=12)) for _ in range(3)]
    rows = []
    for _ in range(n):
        stime = WINDOW_START + rng.randrange(0, 3_500_000)
        proto = rng.choice([6, 6, 6, 17, 1])
        rows.append(
            (
                f"10.{rng.randrange(256)}.{rng.randrange(256)}.{rng.randrange(1, 255)}",
                f"203.0.113.{rng.randrange(1, 255)}",
                rng.randrange(1024, 65535),
                rng.choice([53, 80, 123, 443, 8080]),
                proto,
                rng.choice([2, 18, 24, 27]) if proto == 6 else 0,
                rng.choice(circuits),
                stime,
                stime + rng.randrange(1, 5000),
                rng.randrange(40, 1_000_000),
                rng.randrange(1, 800),
            )
        )
    rows.sort(key=lambda r: r[8])
    con = sqlite3.connect(path)
    con.executescript(SCHEMA)
    con.executemany("INSERT INTO flows VALUES (?,?,?,?,?,?,?,?,?,?,?)", rows)
    con.commit()
    con.close()


if __name__ == "__main__":
    seed(Path(sys.argv[1] if len(sys.argv) > 1 else "data/telemetry.db"))
