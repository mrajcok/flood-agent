"""Seeded synthetic Telemetry Database generator with injected attacks.

    python -m flood_agent.generator [data/telemetry.db] [--seed N] [--scale F]

The data starts at START_MS; the dev server can shift it to end at the current time
(``dev_tools.shift_to_now``). Attack parameters are returned and written to
``<db>.attacks.json`` for the eval set.
"""

import argparse
import json
import math
import random
import sqlite3
from pathlib import Path

START_MS = 1767225600000  # 2026-01-01T00:00:00Z
DAYS = 7
MIN_MS = 60_000
TOTAL_MINUTES = DAYS * 24 * 60
END_MS = START_MS + DAYS * 86_400_000  # the end of the generated data; the eval runner's fixed "now"
BASE_PER_MIN = 190.0  # mean baseline Flow Records per minute at scale 1.0

SCHEMA = """
CREATE TABLE flows (
  sip TEXT, dip TEXT, sport INTEGER, dport INTEGER, proto INTEGER,
  tcp_flags INTEGER, circuit TEXT, stime INTEGER, etime INTEGER,
  bytes INTEGER, pkts INTEGER
);
"""
INDEXES = """
CREATE INDEX flows_etime ON flows(etime);
CREATE INDEX flows_sip ON flows(sip);
CREATE INDEX flows_dip ON flows(dip);
"""

# type -> (start minute offset, duration minutes, flows per minute at scale 1.0)
ATTACK_PLAN = {
    "syn_flood": (1 * 1440 + 14 * 60, 30, 1500),
    "amplification": (3 * 1440 + 3 * 60, 20, 2000),
    "port_scan": (5 * 1440 + 20 * 60, 15, 1500),
}


def _diurnal(minute: int) -> float:
    """Multiplier peaking mid-afternoon UTC, trough in the small hours."""
    h = (minute % 1440) / 60
    return 1 + 0.6 * math.sin(2 * math.pi * (h - 9) / 24)


def _base_count(minute: int, scale: float) -> int:
    return max(0, round(BASE_PER_MIN * scale * _diurnal(minute)))


def expected_rows(scale: float) -> int:
    base = sum(_base_count(m, scale) for m in range(TOTAL_MINUTES))
    return base + sum(round(d * r * scale) for _, d, r in ATTACK_PLAN.values())


def _circuit(rng):
    return "".join(rng.choices("ABCDEFGHJKLMNPQRSTUVWXYZ0123456789", k=12))


def _flow(sip, dip, sport, dport, proto, flags, circuit, etime, dur, nbytes, pkts):
    return (sip, dip, sport, dport, proto, flags, circuit, etime - dur, etime, nbytes, pkts)


def generate(db_path, seed: int = 1, scale: float = 1.0) -> list[dict]:
    """Build the Telemetry Database; return the injected attack records."""
    db_path = Path(db_path)
    rng = random.Random(seed)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    db_path.unlink(missing_ok=True)

    circuits = [_circuit(rng) for _ in range(8)]
    clients = [f"10.{rng.randrange(1, 4)}.{rng.randrange(256)}.{rng.randrange(1, 255)}" for _ in range(2000)]
    servers = [f"203.0.113.{i}" for i in range(1, 255)] + [f"198.51.100.{i}" for i in range(1, 100)]
    server_ports = [443, 443, 443, 80, 80, 53, 123, 8080, 22, 25]

    # Attack parameters are drawn up front so they do not depend on row sampling.
    syn_c, amp_c, scan_c = rng.sample(circuits, 3)
    syn_target, amp_victim = "203.0.113.50", "198.51.100.77"
    scanner, scan_net = "192.0.2.99", "10.20.0."
    reflectors = [f"{rng.randrange(11, 223)}.{rng.randrange(256)}.{rng.randrange(256)}.{rng.randrange(1, 255)}"
                  for _ in range(200)]
    attacks = {}
    for kind, (off, dur, _rate) in ATTACK_PLAN.items():
        attacks[kind] = {"type": kind, "start": START_MS + off * MIN_MS,
                         "end": START_MS + (off + dur) * MIN_MS - 1, "flow_count": 0}
    attacks["syn_flood"].update(
        description="Spoofed-source TCP SYN flood against one service",
        targets=[{"dip": syn_target, "dports": [443]}], circuits=[syn_c], sources=["spoofed (random)"])
    attacks["amplification"].update(
        description="DNS/NTP reflection/amplification: large UDP replies from reflectors to a victim",
        targets=[{"dip": amp_victim, "dports": ["ephemeral"]}], circuits=[amp_c],
        sources=reflectors[:5] + ["..."], reflector_count=len(reflectors), reflector_ports=[53, 123])
    attacks["port_scan"].update(
        description="Single-source TCP SYN scan sweeping many ports across a /24",
        targets=[{"dip": f"{scan_net}0/24", "dports": ["1-1024"]}], circuits=[scan_c], sources=[scanner])

    def baseline(etime):
        client, server = rng.choice(clients), rng.choice(servers)
        dport = rng.choice(server_ports)
        c = rng.choice(circuits)
        dur = rng.randrange(1, 30_000)
        if dport in (53, 123):
            proto, flags, size = 17, 0, rng.randrange(60, 600)
        elif rng.random() < 0.03:
            proto, flags, size = 1, 0, rng.randrange(64, 1500)
        else:
            proto, flags, size = 6, rng.choice([2, 18, 24, 27, 27, 27, 30]), None
        pkts = rng.randrange(1, 40)
        nbytes = size * pkts if size else pkts * rng.randrange(60, 1400)
        sport = rng.randrange(1024, 65535) if proto != 1 else 0
        dp = dport if proto != 1 else 0
        if rng.random() < 0.5:
            return _flow(client, server, sport, dp, proto, flags, c, etime, dur, nbytes, pkts)
        return _flow(server, client, dp, sport, proto, flags, c, etime, dur, nbytes, pkts)

    def attack_flow(kind, etime):
        if kind == "syn_flood":
            sip = f"{rng.randrange(1, 223)}.{rng.randrange(256)}.{rng.randrange(256)}.{rng.randrange(1, 255)}"
            return _flow(sip, syn_target, rng.randrange(1024, 65535), 443, 6, 2, syn_c, etime,
                         rng.randrange(0, 50), rng.randrange(40, 180), rng.randrange(1, 4))
        if kind == "amplification":
            pkts = rng.randrange(3, 40)
            return _flow(rng.choice(reflectors), amp_victim, rng.choice([53, 123]), rng.randrange(1024, 65535),
                         17, 0, amp_c, etime, rng.randrange(1, 2000), pkts * rng.randrange(450, 1500), pkts)
        return _flow(scanner, f"{scan_net}{rng.randrange(1, 255)}", rng.randrange(40000, 65535),
                     rng.randrange(1, 1025), 6, 2, scan_c, etime, rng.randrange(0, 20),
                     rng.choice([40, 44, 60]), 1)

    con = sqlite3.connect(db_path)
    con.executescript(SCHEMA)
    insert = "INSERT INTO flows VALUES (?,?,?,?,?,?,?,?,?,?,?)"
    for minute in range(TOTAL_MINUTES):
        t0 = START_MS + minute * MIN_MS
        batch = [(t0 + rng.randrange(MIN_MS), None) for _ in range(_base_count(minute, scale))]
        for kind, (off, dur, rate) in ATTACK_PLAN.items():
            if off <= minute < off + dur:
                n = round(rate * scale)
                batch += [(t0 + rng.randrange(MIN_MS), kind) for _ in range(n)]
                attacks[kind]["flow_count"] += n
        batch.sort(key=lambda b: b[0])  # inserted in etime order (Time Key stand-in)
        con.executemany(insert, [attack_flow(k, t) if k else baseline(t) for t, k in batch])
    con.executescript(INDEXES)
    con.commit()
    con.close()

    out = list(attacks.values())
    Path(str(db_path) + ".attacks.json").write_text(json.dumps(out, indent=2) + "\n")
    return out


def main(argv=None) -> None:
    p = argparse.ArgumentParser(description=(__doc__ or "").split("\n")[0])
    p.add_argument("db", nargs="?", default="data/telemetry.db")
    p.add_argument("--seed", type=int, default=1)
    p.add_argument("--scale", type=float, default=1.0)
    a = p.parse_args(argv)
    attacks = generate(a.db, seed=a.seed, scale=a.scale)
    print(f"wrote {a.db}; attacks: " + ", ".join(f"{x['type']} ({x['flow_count']} flows)" for x in attacks))


if __name__ == "__main__":
    main()
