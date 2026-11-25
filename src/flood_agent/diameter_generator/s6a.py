"""Baseline S6a traffic between MMEs and HSSs.

A home Subscriber's requests go to the home HSS, from a home MME or, for an outbound roamer, from a
partner's. An inbound roamer's go from a home MME to its partner's HSS.
"""

from collections.abc import Callable
from itertools import accumulate

from flood_agent.diameter_generator.network import Node, Subscriber
from flood_agent.diameter_generator.records import AIR, CLR, DSR, IDR, NOR, PUR, S6A, SUCCESS, ULR, Record, World
from flood_agent.generator_clock import END_MS, MIN_MS, START_MS, TOTAL_MINUTES, diurnal

ERROR_RATE = 0.022  # of the MME's requests, which are nearly all of them: about 2% of S6a records
USER_UNKNOWN, ROAMING_NOT_ALLOWED, UNKNOWN_EPS_SUBSCRIPTION = 5001, 5004, 5420  # experimental_result_code
CANCEL_MME_UPDATE = 0  # cancellation_type

# What a Subscriber does in the week: some attaches, then a few of the rest.
ATTACHES = (3, 6)
OTHERS = (1, 4)
OTHER_WEIGHTS = {"move": 30, "notify": 30, "purge": 20, "insert data": 15, "delete data": 5}
MINUTE_WEIGHTS = list(accumulate(diurnal(m) for m in range(TOTAL_MINUTES)))  # the daily load curve
LAST_START = END_MS - MIN_MS  # leaves time for the answers within the generated week
EVENT_GAP_MS = 60_000  # from the start of an event other than an attach to the next one's


def attach(world: World, subscriber: Subscriber, mme: Node, stime: int) -> Record:
    """An AIR, then a ULR if the AIR succeeds. Returns the last of the two."""
    air = _mme_request(world, subscriber, AIR, mme, stime)
    if air.result_code != SUCCESS:
        return air
    return _mme_request(world, subscriber, ULR, mme, air.etime + world.rng.randrange(5, 200))


def _identifiers(subscriber: Subscriber) -> dict:
    return {"imsi": subscriber.imsi, "home_plmn_id": subscriber.home.plmn}


def _mme_request(world: World, subscriber: Subscriber, command: int, mme: Node, stime: int) -> Record:
    """One request from an MME to the Subscriber's HSS."""
    columns = _identifiers(subscriber)
    if command in (AIR, ULR):
        columns["visited_plmn_id"] = subscriber.visited.plmn
    if command == ULR:
        columns.update(rat_type=subscriber.rat_type, imei=subscriber.imei, msisdn=subscriber.msisdn)
    if world.rng.random() < ERROR_RATE:
        errors = [USER_UNKNOWN]
        if command in (AIR, ULR):
            errors.append(UNKNOWN_EPS_SUBSCRIPTION)
            if subscriber.visited is not subscriber.home:
                errors.append(ROAMING_NOT_ALLOWED)
        columns.update(result_code=None, experimental_result_code=world.rng.choice(errors), msisdn=None)
    # An MME learns the HSS's host name from its first answers, so an attach names none.
    return world.transact(stime, S6A, command, mme, subscriber.home.hss,
                          names_destination=command not in (AIR, ULR), **columns)


def _hss_request(world: World, subscriber: Subscriber, command: int, mme: Node, stime: int, **columns) -> None:
    """One request from the Subscriber's HSS to an MME, which must name its destination."""
    if command == IDR:
        columns["msisdn"] = subscriber.msisdn
    world.transact(stime, S6A, command, subscriber.home.hss, mme, names_destination=True,
                   **_identifiers(subscriber), **columns)


def _move(world: World, subscriber: Subscriber, old: Node, stime: int) -> Node:
    """The Subscriber moves to another MME of the visited network; returns the MME now serving it.

    The home HSS then cancels the registration at the old MME. A partner HSS's cancellations are
    left out, so every baseline CLR comes from the home realm."""
    new = world.rng.choice([m for m in subscriber.visited.mmes if m != old])
    update = _mme_request(world, subscriber, ULR, new, stime)
    if update.result_code != SUCCESS:
        return old
    if subscriber.home is world.home:
        _hss_request(world, subscriber, CLR, old, update.etime + world.rng.randrange(5, 200),
                     cancellation_type=CANCEL_MME_UPDATE)
    return new


# An event of a Subscriber's week, begun at `stime` with `mme` serving it. Returns the MME serving it
# afterwards, and when it is free for its next event.
Event = Callable[[World, Subscriber, Node, int], tuple[Node, int]]


def _attach_event(world: World, subscriber: Subscriber, mme: Node, stime: int) -> tuple[Node, int]:
    return mme, attach(world, subscriber, mme, stime).etime + 1000


def _move_event(world: World, subscriber: Subscriber, mme: Node, stime: int) -> tuple[Node, int]:
    return _move(world, subscriber, mme, stime), stime + EVENT_GAP_MS


def _one_request(send: Callable[[World, Subscriber, int, Node, int], object], command: int) -> Event:
    """The event of one request, sent by `send`."""

    def event(world: World, subscriber: Subscriber, mme: Node, stime: int) -> tuple[Node, int]:
        send(world, subscriber, command, mme, stime)
        return mme, stime + EVENT_GAP_MS

    return event


EVENTS: dict[str, Event] = {
    "attach": _attach_event,
    "move": _move_event,
    "notify": _one_request(_mme_request, NOR),
    "purge": _one_request(_mme_request, PUR),
    "insert data": _one_request(_hss_request, IDR),
    "delete data": _one_request(_hss_request, DSR),
}


def baseline(world: World) -> None:
    rng = world.rng
    for subscriber in world.subscribers:
        events = ["attach"] * (rng.randint(*ATTACHES) - 1)
        events += rng.choices(list(OTHER_WEIGHTS), list(OTHER_WEIGHTS.values()), k=rng.randint(*OTHERS))
        rng.shuffle(events)
        events.insert(0, "attach")  # a Subscriber's week starts by attaching
        minutes = rng.choices(range(TOTAL_MINUTES), cum_weights=MINUTE_WEIGHTS, k=len(events))
        times = sorted(START_MS + m * MIN_MS + rng.randrange(MIN_MS) for m in minutes)
        mme = rng.choice(subscriber.visited.mmes)
        free_from = START_MS  # one procedure at a time
        for event, drawn in zip(events, times, strict=True):
            stime = max(drawn, free_from)
            if stime >= LAST_START:
                break
            mme, free_from = EVENTS[event](world, subscriber, mme, stime)
