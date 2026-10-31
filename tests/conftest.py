"""Shared fixtures: tiny seeded Telemetry Database, results DB, scripted fake LLM."""

import copy
import json
import sqlite3
from dataclasses import replace
from pathlib import Path

import pytest

from flood_defender.agent import Agent, AgentConfig, Conversation
from flood_defender.config import Settings
from flood_defender.llm import TextChunk, ToolCallChunk, UsageChunk

SCHEMA = """
CREATE TABLE flows (
  sip TEXT, dip TEXT, sport INTEGER, dport INTEGER, proto INTEGER,
  tcp_flags INTEGER, circuit TEXT, stime INTEGER, etime INTEGER,
  bytes INTEGER, pkts INTEGER
);
CREATE INDEX flows_etime ON flows(etime);
"""

# Deliberately distinctive values so the leak invariant test cannot be fooled
# by coincidental substrings.
FLOWS = [
    ("172.30.41.7", "198.18.77.200", 48213, 8443, 6, 2, "QX7ZK2M9PA41", 1_700_000_000_111, 1_700_000_000_999, 987_654, 321),
    ("172.30.41.8", "198.18.77.200", 51877, 8443, 6, 18, "QX7ZK2M9PA41", 1_700_000_001_111, 1_700_000_001_999, 123_457, 111),
    ("172.30.99.3", "198.18.12.34", 40001, 9053, 17, 0, "RT5NB8W3LC02", 1_700_000_002_111, 1_700_000_002_999, 555_321, 77),
]

TEST_SEMANTIC_LAYER = """\
dialect: sqlite
window_start: 1600000000000
window_end: 1600000100000
time_key: etime
tables:
  flows:
    description: Flow Records.
    columns:
      sip: {type: TEXT, description: Source IPv4, example: "192.0.2.1"}
      dport: {type: INTEGER, description: Destination port, example: 80}
      etime: {type: INTEGER, description: End time epoch ms UTC, example: 1500000000000}
      bytes: {type: INTEGER, unit: bytes, description: Total bytes, example: 1000}
"""


@pytest.fixture
def telemetry_db(tmp_path: Path) -> Path:
    path = tmp_path / "telemetry.db"
    con = sqlite3.connect(path)
    con.executescript(SCHEMA)
    con.executemany("INSERT INTO flows VALUES (?,?,?,?,?,?,?,?,?,?,?)", FLOWS)
    con.commit()
    con.close()
    return path


def all_telemetry_values(path: Path) -> set[str]:
    con = sqlite3.connect(path)
    vals = {str(v) for row in con.execute("SELECT * FROM flows") for v in row}
    con.close()
    return vals


class ScriptedLLM:
    """Fake OpenRouter client. Each script step is a list of chunks, or a
    callable(messages) -> list of chunks. Records every message list it receives."""

    def __init__(self, *steps):
        self.steps = list(steps)
        self.calls: list[list[dict]] = []
        self.tools_seen_names: list[str] = []

    async def stream_chat(self, messages, tools):
        self.calls.append(copy.deepcopy(messages))
        self.tools_seen_names = [t["function"]["name"] for t in tools]
        step = self.steps.pop(0)
        chunks = step(messages) if callable(step) else step
        for c in chunks:
            yield c
        yield UsageChunk(prompt_tokens=100, completion_tokens=20, cost=0.0012)

    def all_sent_text(self) -> str:
        return json.dumps(self.calls)


def tool_call(name: str, call_id: str = "c1", **args) -> ToolCallChunk:
    return ToolCallChunk(id=call_id, name=name, arguments=json.dumps(args))


def last_tool_result(messages) -> dict:
    return json.loads([m for m in messages if m["role"] == "tool"][-1]["content"])


@pytest.fixture
def semantic_layer_path(tmp_path: Path) -> Path:
    p = tmp_path / "semantic_layer.yaml"
    p.write_text(TEST_SEMANTIC_LAYER)
    return p


@pytest.fixture(autouse=True)
def isolated_settings(monkeypatch):
    """Settings must not pick up a developer's .env file or exported variables."""
    monkeypatch.setitem(Settings.model_config, "env_file", None)
    for name in Settings.model_fields:
        monkeypatch.delenv(name.upper(), raising=False)


def config_for_test(tmp_path, telemetry_db, semantic_layer, **overrides) -> AgentConfig:
    """The defaults.toml settings, pointed at this test's files."""
    return replace(
        Settings().agent_config(),
        telemetry_db=telemetry_db,
        results_db=tmp_path / "results.db",
        app_db=tmp_path / "app.db",
        semantic_layer=semantic_layer,
        model="test/model",
        **overrides,
    )


def query_limits() -> dict:
    """timeout_s and max_rows for calling queries.run_query directly."""
    cfg = Settings().agent_config()
    return {"timeout_s": cfg.query_timeout_s, "max_rows": cfg.max_result_rows}


@pytest.fixture
def make_agent(tmp_path, telemetry_db, semantic_layer_path):
    def _make(llm, **overrides) -> Agent:
        return Agent(config_for_test(tmp_path, telemetry_db, semantic_layer_path, **overrides), llm)

    return _make


async def collect(agent: Agent, message: str, conversation: Conversation | None = None):
    return [e async for e in agent.run_turn(conversation or Conversation(), message)]


__all__ = ["TextChunk"]
