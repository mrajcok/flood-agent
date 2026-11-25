"""Gx data sessions between the PCEF and the PCRF, each mirrored by a Gy session with the OCS.

A fifth of the `flows` client IPs are UE IPs. Each of those is held by one Gx session after another
all week, passed between home Subscribers with no gap, so each of its flows belongs to exactly one
Subscriber. Every other session gets an address from 10.64.0.0/16, which `flows` never uses.
"""

from bisect import bisect_left
from itertools import count

from flood_agent.diameter_generator.network import (
    DIAMETER_PORT,
    Node,
    Subscriber,
    ephemeral_port,
    new_session_id,
    subnet_of,
)
from flood_agent.diameter_generator.records import CCR, GX, GY, SUCCESS, Record, World
from flood_agent.diameter_generator.s6a import MINUTE_WEIGHTS
from flood_agent.generator_clock import END_MS, MIN_MS, START_MS

INITIAL, UPDATE, TERMINATION = 1, 2, 3  # cc_request_type
CREDIT_LIMIT_REACHED = 4012  # result_code
CREDIT_LIMIT_RATE = 0.025  # of the Gy requests that can be refused: about 2% of Gy records

SHARED_CLIENT_SHARE = 0.20  # of the distinct `flows` client IPs
HANDOVERS = (2, 4)  # times a shared client IP passes to another Subscriber in the week
HOUR_MS = 60 * MIN_MS
HELD_HOURS = (10, 18)  # how long a session on a shared client IP is planned to last
SESSIONS = (2, 6)  # per home Subscriber, besides those on shared client IPs
UPDATES = (1, 6)  # CCR-Us per session, fewer if it is too short
MAX_SESSION_MS = 23 * HOUR_MS  # under the 24 h cap even when the TERMINATION is sent again
FINAL_LATENCY_MS = 20  # of a TERMINATION answered in the week's last millisecond
APNS, APN_WEIGHTS = ("internet", "ims"), (80, 20)

Servers = tuple[tuple[int, Node], ...]  # each interface, with the server the PCEF asks on it: Gx first


def _nodes(world: World) -> tuple[Node, Servers]:
    """The PCEF, PCRF and OCS of the home network."""
    hss = world.home.hss
    subnet = subnet_of(hss.ip)

    def node(name: str, last: int, port: int) -> Node:
        return Node(f"{name}.{world.home.realm}", world.home.realm, f"{subnet}.{last}", port, hss.proto)

    pcef = node("pgw01", 40, ephemeral_port(world.rng))
    return pcef, ((GX, node("pcrf01", 30, DIAMETER_PORT)), (GY, node("ocs01", 31, DIAMETER_PORT)))


def _time_between(world: World, lo: int, hi: int) -> int:
    """A time in [lo, hi], drawn along the daily load curve."""
    first, last = (lo - START_MS) // MIN_MS, (hi - START_MS) // MIN_MS
    below = MINUTE_WEIGHTS[first - 1] if first else 0.0
    minute = bisect_left(MINUTE_WEIGHTS, world.rng.uniform(below, MINUTE_WEIGHTS[last]), first, last)
    return min(hi, max(lo, START_MS + minute * MIN_MS + world.rng.randrange(MIN_MS)))


def _credit_control(
    world: World, interface: int, pcef: Node, server: Node, columns: dict, start: int, end: int, *,
    closes_week: bool, rat_type: int | None = None,
) -> tuple[Record, Record]:
    """One session on one interface: a CCR-I at `start`, some CCR-Us, and a CCR-T sent at `end`.
    With `closes_week`, the CCR-T's answer arrives in the week's last millisecond instead.
    `rat_type` goes on the CCR-I alone.

    Returns the answered CCR-I and CCR-T."""
    rng = world.rng
    session_id = new_session_id(rng, pcef.host)
    numbers = count()

    def request(stime: int, request_type: int, **more) -> Record:
        refusable = interface == GY and request_type != TERMINATION
        result = CREDIT_LIMIT_REACHED if refusable and rng.random() < CREDIT_LIMIT_RATE else SUCCESS
        return world.transact(
            stime, interface, CCR, pcef, server, session_id=session_id, names_destination=True, result_code=result,
            cc_request_type=request_type, cc_request_number=next(numbers), **columns, **more,
        )

    initial = previous = request(start, INITIAL, rat_type=rat_type)
    if end - start > MIN_MS:
        for drawn in sorted(_time_between(world, start, end) for _ in range(rng.randint(*UPDATES))):
            stime = max(drawn, previous.etime + 1000)
            if stime < end - 10_000:  # answered, even if sent again, before the session ends
                previous = request(stime, UPDATE)
    if closes_week:
        return initial, request(END_MS - 1 - FINAL_LATENCY_MS, TERMINATION, latency_ms=FINAL_LATENCY_MS)
    return initial, request(max(end, previous.etime + 1000), TERMINATION)


def _session(
    world: World, pcef: Node, servers: Servers, subscriber: Subscriber, ue_ip: str, apn: str, start: int, end: int,
    *, closes_week: bool = False,
) -> Record:
    """A Gx session and its Gy mirror. Returns the Gx session's answered CCR-T."""
    # The probe correlates a session, so every record of it has these, not only the CCR-I.
    columns = {"imsi": subscriber.imsi, "msisdn": subscriber.msisdn, "home_plmn_id": subscriber.home.plmn,
               "ue_ip_address": ue_ip, "apn": apn}
    (gx, pcrf), (gy, ocs) = servers
    initial, termination = _credit_control(
        world, gx, pcef, pcrf, columns, start, end, closes_week=closes_week, rat_type=subscriber.rat_type
    )
    # The PCEF asks the OCS once the PCRF has answered, and closes the two together.
    _credit_control(world, gy, pcef, ocs, columns, initial.etime + world.rng.randrange(5, 200),
                    min(end + world.rng.randrange(5, 200), END_MS - 1), closes_week=closes_week)
    return termination


def _hold_all_week(world: World, pcef: Node, servers: Servers, subscribers: list[Subscriber], ue_ip: str) -> None:
    """One session after another on `ue_ip`, each starting a millisecond after the one before ends."""
    rng = world.rng
    ends = []  # of every session but the last, which ends with the week
    planned = START_MS
    while END_MS - planned > MAX_SESSION_MS:
        planned += rng.randrange(HELD_HOURS[0] * HOUR_MS, HELD_HOURS[1] * HOUR_MS)
        ends.append(planned)
    handovers = rng.randint(*HANDOVERS)
    changes = set(rng.sample(range(1, len(ends) + 1), handovers))  # the sessions a new holder starts with
    holders = iter(rng.sample(subscribers, handovers + 1))
    holder = next(holders)
    start = START_MS
    for i, end in enumerate([*ends, END_MS - 1]):
        if i in changes:
            holder = next(holders)
        termination = _session(world, pcef, servers, holder, ue_ip, "internet", start, end,
                               closes_week=i == len(ends))
        start = termination.etime + 1


def data_sessions(world: World) -> None:
    rng = world.rng
    pcef, servers = _nodes(world)
    home = [s for s in world.subscribers if s.home is world.home]  # inbound roamers use S6a only
    # An RNG of its own, so that which client IPs are shared depends on the seed and not on the scale.
    picker = world.fork("shared client ips").rng
    for ue_ip in picker.sample(world.client_ips, round(SHARED_CLIENT_SHARE * len(world.client_ips))):
        _hold_all_week(world, pcef, servers, home, ue_ip)
    addresses = count(1)
    for subscriber in home:
        for _ in range(rng.randint(*SESSIONS)):
            n = next(addresses) % 65_535
            start = _time_between(world, START_MS, END_MS - 10 * MIN_MS)
            length = min(MAX_SESSION_MS, max(MIN_MS, round(rng.lognormvariate(0, 1.5) * HOUR_MS)))
            _session(world, pcef, servers, subscriber, f"10.64.{n // 256}.{n % 256}",
                     rng.choices(APNS, APN_WEIGHTS)[0], start, min(start + length, END_MS - MIN_MS))
