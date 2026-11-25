"""The HSS signaling flood: one partner host floods AIRs at the home HSS, which sheds load.

While it lasts, the HSS refuses many requests with DIAMETER_TOO_BUSY and leaves some unanswered, the
flood's and those of MMEs trying to authenticate home Subscribers. The latter are requests of the
attack's own, so the baseline's records stay as they were. RFC 6733 allows that answer only for a
request naming the server in Destination-Host, so every request here names the HSS.
"""

from flood_agent.diameter_generator.network import Node, Subscriber
from flood_agent.diameter_generator.records import AIR, S6A, SUCCESS, World
from flood_agent.generator_clock import MIN_MS, START_MS

TOO_BUSY = 3004  # result_code
START = START_MS + (4 * 1440 + 16 * 60) * MIN_MS  # day 4, 16:00
MINUTES = 20
END = START + MINUTES * MIN_MS - 1
SEND_END = END - 10_000  # leaves time for the retransmissions and answers within the window
# AIRs a minute for each Subscriber: 600 and 60 at scale 1.0, where the baseline sends the HSS about 10.
FLOOD_RATE, OTHERS_RATE = 0.12, 0.012
# The shares of each kind that the HSS refuses, and that first get no answer.
FLOOD_REFUSED, FLOOD_UNANSWERED = 0.7, 0.1
OTHERS_REFUSED, OTHERS_UNANSWERED = 1.0, 0.3


def hss_flood(world: World) -> None:
    world = world.fork("hss_flood")
    rng, home = world.rng, world.home
    at_home = [s for s in world.subscribers if s.home is home]
    # The flooding host belongs to a partner's network, so it asks about the home Subscribers roaming there.
    roamers = [s for s in at_home if s.visited is not home]
    partner = rng.choice([p for p in world.partners if any(s.visited is p for s in roamers)])
    roamers = [s for s in roamers if s.visited is partner]
    mme = partner.mmes[0]
    host = Node(f"mmec99.mmegi8001.mme.{partner.realm}", partner.realm,
                mme.ip.rsplit(".", 1)[0] + ".99", rng.randrange(32768, 61000), mme.proto)

    for stime in _send_times(world, OTHERS_RATE):
        subscriber = rng.choice(at_home)
        _air(world, stime, subscriber, rng.choice(subscriber.visited.mmes), OTHERS_REFUSED, OTHERS_UNANSWERED)
    first = len(world.records)
    for stime in _send_times(world, FLOOD_RATE):
        _air(world, stime, rng.choice(roamers), host, FLOOD_REFUSED, FLOOD_UNANSWERED)
    world.attacks.append({
        "type": "diameter_hss_flood", "start": START, "end": END,
        "description": "HSS signaling flood: one partner host floods AIRs at the home HSS, which sheds load",
        "source_realm": host.realm, "source_host": host.host, "record_count": len(world.records) - first})


def _send_times(world: World, rate: float) -> range:
    """Evenly spaced request times over the window, `rate` a minute for each Subscriber."""
    count = round(rate * len(world.subscribers) * MINUTES)
    return range(START, SEND_END, (SEND_END - START) // count + 1)


def _air(world: World, stime: int, subscriber: Subscriber, mme: Node, refused: float, unanswered: float) -> None:
    """One AIR to the overloaded HSS. A retransmission gets the answer its first request missed."""
    result = TOO_BUSY if world.rng.random() < refused else SUCCESS
    record = world.transact(stime, S6A, AIR, mme, world.home.hss, names_destination=True, result_code=result,
                            imsi=subscriber.imsi, home_plmn_id=subscriber.home.plmn,
                            visited_plmn_id=subscriber.visited.plmn)
    if record.status == "Answered" and world.rng.random() < unanswered:
        world.time_out(record)
