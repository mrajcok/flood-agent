"""The Diameter Transaction Record, and the World that records each request and its answer."""

import dataclasses
import random
from dataclasses import dataclass, field

from flood_agent.diameter_generator.network import (
    Network,
    Node,
    Subscriber,
    home_network,
    partner_networks,
    subscribers,
)

SCHEMA = """
CREATE TABLE diameter (
  stime INTEGER, etime INTEGER, latency_ms REAL, status TEXT, session_id TEXT, hop_by_hop_id INTEGER,
  end_to_end_id INTEGER, application_id INTEGER, command_code INTEGER, origin_host TEXT, origin_realm TEXT,
  destination_host TEXT, destination_realm TEXT, sip TEXT, dip TEXT, sport INTEGER, dport INTEGER,
  proto INTEGER, result_code INTEGER, experimental_result_code INTEGER, imsi TEXT, msisdn TEXT, imei TEXT,
  home_plmn_id TEXT, visited_plmn_id TEXT, ue_ip_address TEXT, apn TEXT, cc_request_type INTEGER,
  cc_request_number INTEGER, rat_type INTEGER, cancellation_type INTEGER
);
"""
INDEXES = """
CREATE INDEX diameter_etime ON diameter(etime);
CREATE INDEX diameter_imsi ON diameter(imsi);
CREATE INDEX diameter_ue_ip_address ON diameter(ue_ip_address);
"""

S6A, GX, GY = 16777251, 16777238, 4  # application_id
ULR, CLR, AIR, IDR, DSR, PUR, NOR = 316, 317, 318, 319, 320, 321, 323  # command_code
CCR = 272  # command_code, Gx and Gy
SUCCESS = 2001
TIMEOUT_MS = 5000  # the probe's answer timeout
TIMEOUT_RATE = 0.003


@dataclass(kw_only=True)
class Record:
    """One `diameter` row. A field left at None is a column that doesn't apply to it."""

    stime: int
    etime: int
    latency_ms: float | None
    status: str
    session_id: str
    hop_by_hop_id: int
    end_to_end_id: int
    application_id: int
    command_code: int
    origin_host: str
    origin_realm: str
    destination_host: str | None
    destination_realm: str
    sip: str
    dip: str
    sport: int
    dport: int
    proto: int
    result_code: int | None = None
    experimental_result_code: int | None = None
    imsi: str | None = None
    msisdn: str | None = None
    imei: str | None = None
    home_plmn_id: str | None = None
    visited_plmn_id: str | None = None
    ue_ip_address: str | None = None
    apn: str | None = None
    cc_request_type: int | None = None
    cc_request_number: int | None = None
    rat_type: int | None = None
    cancellation_type: int | None = None
    names_destination: bool = False  # not a column: the request carried Destination-Host


COLUMNS = tuple(f.name for f in dataclasses.fields(Record) if f.name != "names_destination")


@dataclass
class World:
    """What the sources of traffic share: the networks, the Subscribers and the records so far."""

    rng: random.Random
    seed: int
    home: Network
    partners: tuple[Network, ...]
    subscribers: tuple[Subscriber, ...]
    records: list[Record] = field(default_factory=list)
    attacks: list[dict] = field(default_factory=list)
    client_ips: tuple[str, ...] = ()  # the distinct client IPs of `flows`, sorted

    @classmethod
    def build(cls, seed: int, scale: float, client_ips: tuple[str, ...] = ()) -> "World":
        rng = random.Random(seed + 5)
        home, partners = home_network(rng), partner_networks(rng)
        return cls(rng, seed, home, partners, subscribers(rng, home, partners, scale), client_ips=client_ips)

    def fork(self, name: str) -> "World":
        """This World with an RNG of its own, adding to the same records and attacks."""
        return dataclasses.replace(self, rng=random.Random(f"diameter {name} {self.seed}"))

    def _latency(self, origin: Node, destination: Node) -> float:
        roaming = origin.realm != destination.realm
        return round(self.rng.uniform(40, 250) if roaming else self.rng.uniform(2, 40), 3)

    def transact(
        self,
        stime: int,
        application_id: int,
        command_code: int,
        origin: Node,
        destination: Node,
        *,
        session_id: str | None = None,
        names_destination: bool = False,
        result_code: int | None = SUCCESS,
        experimental_result_code: int | None = None,
        latency_ms: float | None = None,
        **columns,
    ) -> Record:
        """Record one request from `origin` to `destination`, and its answer.

        `session_id` defaults to a new Session-Id. `columns` sets other `diameter` columns. A few
        requests time out and are sent again. Returns the answered record, which is then the
        retransmission, so that what follows a request can be timed from its `etime`. Given
        `latency_ms`, the answer takes that long and the request does not time out."""
        may_time_out = latency_ms is None
        latency = self._latency(origin, destination) if latency_ms is None else latency_ms
        record = Record(
            stime=stime, etime=stime + round(latency), latency_ms=latency, status="Answered",
            session_id=session_id or f"{origin.host};{self.rng.getrandbits(32)};{self.rng.getrandbits(32)}",
            hop_by_hop_id=self.rng.getrandbits(32), end_to_end_id=self.rng.getrandbits(32),
            application_id=application_id, command_code=command_code,
            origin_host=origin.host, origin_realm=origin.realm,
            destination_host=destination.host, destination_realm=destination.realm,
            sip=origin.ip, dip=destination.ip, sport=origin.port, dport=destination.port, proto=origin.proto,
            result_code=result_code, experimental_result_code=experimental_result_code,
            names_destination=names_destination, **columns,
        )
        self.records.append(record)
        return self.time_out(record) if may_time_out and self.rng.random() < TIMEOUT_RATE else record

    def time_out(self, record: Record) -> Record:
        """Take the answer away from `record`, and record the request being sent again.

        Returns the retransmission, which gets the answer instead."""
        latency = record.latency_ms
        assert latency is not None, "the record has already timed out"
        stime = record.stime + TIMEOUT_MS + self.rng.randrange(1, 50)
        again = dataclasses.replace(
            record, stime=stime, etime=stime + round(latency), status="Retransmission",
            hop_by_hop_id=self.rng.getrandbits(32),
        )
        self.records.append(again)
        record.status, record.etime = "Timeout", record.stime + TIMEOUT_MS
        record.latency_ms = record.result_code = record.experimental_result_code = None
        if record.command_code == ULR:
            record.msisdn = None  # it comes with the answer's subscription data
        if not record.names_destination:
            record.destination_host = None  # only the answer would have shown it
        return again
