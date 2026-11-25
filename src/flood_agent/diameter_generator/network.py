"""The Diameter nodes of the home network and its roaming partners, and the Subscribers."""

import random
from dataclasses import dataclass

DIAMETER_PORT = 3868
SCTP, TCP = 132, 6
EUTRAN, EUTRAN_NB_IOT, LTE_M = 1004, 1005, 1007

SUBSCRIBERS = 5000  # at scale 1.0
INBOUND_SHARE = 0.10  # of all Subscribers: partners' Subscribers visiting the home network
OUTBOUND_SHARE = 0.05  # of all Subscribers: home Subscribers visiting a partner


@dataclass(frozen=True)
class Node:
    """A Diameter peer. `port` is its end of the transport: 3868 on an HSS, ephemeral on an MME."""

    host: str
    realm: str
    ip: str
    port: int
    proto: int


@dataclass(frozen=True)
class Network:
    plmn: str  # MCC+MNC digits
    realm: str
    hss: Node
    mmes: tuple[Node, ...]


@dataclass(frozen=True)
class Subscriber:
    imsi: str
    msisdn: str
    imei: str
    home: Network
    visited: Network  # the network serving the Subscriber; `home` unless roaming
    rat_type: int


def _network(rng: random.Random, mcc: str, mnc: str, subnet: str, mmes: int, proto: int) -> Network:
    realm = f"epc.mnc{mnc.zfill(3)}.mcc{mcc}.3gppnetwork.org"
    hss = Node(f"hss01.{realm}", realm, f"{subnet}.10", DIAMETER_PORT, proto)
    nodes = tuple(
        Node(f"mmec{i:02}.mmegi8001.mme.{realm}", realm, f"{subnet}.{20 + i}", rng.randrange(32768, 61000), proto)
        for i in range(1, mmes + 1)
    )
    return Network(mcc + mnc, realm, hss, nodes)


def home_network(rng: random.Random) -> Network:
    return _network(rng, "001", "01", "10.100.0", mmes=4, proto=SCTP)  # the test MCC and MNC


def partner_networks(rng: random.Random) -> tuple[Network, ...]:
    partners = (("262", "01", SCTP), ("208", "01", SCTP), ("234", "15", SCTP), ("310", "410", TCP))
    return tuple(
        _network(rng, mcc, mnc, f"198.18.{i}", mmes=2, proto=proto)
        for i, (mcc, mnc, proto) in enumerate(partners, 1)
    )


def _imei(rng: random.Random) -> str:
    """15 digits: a test TAC (reporting body 00) and a Luhn check digit."""
    body = "00" + "".join(rng.choices("0123456789", k=12))
    digits = (int(d) * (2 if i % 2 else 1) for i, d in enumerate(body))
    return body + str(-sum(d // 10 + d % 10 for d in digits) % 10)


def subscribers(
    rng: random.Random, home: Network, partners: tuple[Network, ...], scale: float
) -> tuple[Subscriber, ...]:
    total = max(40, round(SUBSCRIBERS * scale))
    inbound, outbound = round(total * INBOUND_SHARE), round(total * OUTBOUND_SHARE)
    out = []
    for i in range(total):
        own = visited = home
        if i < inbound:
            own = partners[i % len(partners)]
        elif i < inbound + outbound:
            visited = partners[i % len(partners)]
        # Serial numbers keep identifiers unique. +999 is an unassigned country code.
        imsi = own.plmn + f"{999_000_000 + i:0{15 - len(own.plmn)}}"
        rat = rng.choices((EUTRAN, EUTRAN_NB_IOT, LTE_M), (90, 4, 6))[0]
        out.append(Subscriber(imsi, f"999{i:08}", _imei(rng), own, visited, rat))
    return tuple(out)
