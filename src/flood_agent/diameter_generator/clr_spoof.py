"""The spoofed Cancel-Location DoS: a host in a foreign realm poses as the home HSS.

It tells the home MMEs that home Subscribers' subscriptions are withdrawn. The MMEs comply, and the
Subscribers' devices attach again.
"""

from flood_agent.diameter_generator.network import DIAMETER_PORT, SCTP, SUBSCRIBERS, Node
from flood_agent.diameter_generator.records import CLR, S6A, SUCCESS, ULR, World
from flood_agent.diameter_generator.s6a import attach
from flood_agent.generator_clock import MIN_MS, START_MS

START = START_MS + (2 * 1440 + 22 * 60) * MIN_MS  # day 2, 22:00
DURATION = 30 * MIN_MS
VICTIMS = 300  # at scale 1.0
SUBSCRIPTION_WITHDRAWAL = 2  # cancellation_type
REATTACH_DELAY = (2_000, 20_000)  # ms from a cancellation, or a failed attach, to the next attach

_REALM = "epc.mnc099.mcc999.3gppnetwork.org"  # fictional: MCC 999 is for private networks
ATTACKER = Node(f"hss01.{_REALM}", _REALM, "198.19.66.6", DIAMETER_PORT, SCTP)


def attack(world: World) -> None:
    world = world.fork("clr spoof")
    rng = world.rng
    # The successful location updates of the week so far, oldest first: which MME served whom, when.
    updates: dict[str | None, list[tuple[int, str]]] = {}
    for r in sorted(world.records, key=lambda r: r.etime):
        if r.command_code == ULR and r.result_code == SUCCESS:
            updates.setdefault(r.imsi, []).append((r.etime, r.origin_host))
    attached = [
        s for s in world.subscribers
        if s.home is world.home and s.visited is world.home
        and s.imsi in updates and updates[s.imsi][0][0] < START
    ]
    victims = rng.sample(attached, round(VICTIMS * len(world.subscribers) / SUBSCRIBERS))
    mmes = {mme.host: mme for mme in world.home.mmes}
    # The last minute is left for the victims to attach again within the window.
    times = sorted(START + rng.randrange(DURATION - MIN_MS) for _ in victims)
    first = len(world.records)
    for victim, stime in zip(victims, times, strict=True):
        mme = mmes[[host for etime, host in updates[victim.imsi] if etime <= stime][-1]]
        cancel = world.transact(
            stime, S6A, CLR, ATTACKER, mme, names_destination=True, imsi=victim.imsi,
            home_plmn_id=victim.home.plmn, cancellation_type=SUBSCRIPTION_WITHDRAWAL,
        )
        last = attach(world, victim, mme, cancel.etime + rng.randrange(*REATTACH_DELAY))
        while not (last.command_code == ULR and last.result_code == SUCCESS):
            last = attach(world, victim, mme, last.etime + rng.randrange(*REATTACH_DELAY))
    world.attacks.append({
        "type": "diameter_clr_spoof", "start": START, "end": START + DURATION - 1,
        "description": "Spoofed Cancel-Location DoS: a foreign host posing as the home HSS cancels "
                       "home Subscribers' registrations",
        "source_realm": ATTACKER.realm, "source_host": ATTACKER.host,
        "record_count": sum(r.origin_host == ATTACKER.host for r in world.records[first:]),
        "victim_count": len(victims),
    })
