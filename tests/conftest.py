"""Shared fixtures: tiny seeded Telemetry Database, results DB, scripted fake LLM."""

import copy
import json
import os
import shutil
import sqlite3
from contextlib import ExitStack, asynccontextmanager
from dataclasses import replace
from pathlib import Path

import anyio
import pytest
from fastapi.testclient import TestClient
from mcp.shared.memory import create_client_server_memory_streams

from flood_defender.agent import Agent, AgentConfig, Conversation
from flood_defender.config import Settings
from flood_defender.llm import TextChunk, ToolCallChunk, UsageChunk
from flood_defender.mcp_server import ServerSettings, create_server
from flood_defender.web import create_app

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


@pytest.fixture(scope="session")
def telemetry_db_template(tmp_path_factory) -> Path:
    path = tmp_path_factory.mktemp("template") / "telemetry.db"
    con = sqlite3.connect(path)
    con.executescript(SCHEMA)
    con.executemany("INSERT INTO flows VALUES (?,?,?,?,?,?,?,?,?,?,?)", FLOWS)
    con.commit()
    con.close()
    return path


@pytest.fixture
def telemetry_db(tmp_path: Path, telemetry_db_template: Path) -> Path:
    """A per-test copy of the session's test Telemetry Database."""
    return Path(shutil.copy(telemetry_db_template, tmp_path / "telemetry.db"))


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


@asynccontextmanager
async def in_process_transport(settings: ServerSettings):
    """Client (read, write) streams to the MCP server running in this process: the same tools
    and settings as the stdio subprocess, without its 0.4 s interpreter start."""
    server = create_server(settings)._mcp_server  # as the SDK's own in-memory helper does
    async with create_client_server_memory_streams() as (client_streams, (server_read, server_write)):
        async with anyio.create_task_group() as tg:
            tg.start_soon(lambda: server.run(server_read, server_write, server.create_initialization_options()))
            try:
                yield client_streams
            finally:
                tg.cancel_scope.cancel()


@pytest.fixture(autouse=True)
def in_process_mcp_server(request, monkeypatch):
    """Agents run their MCP server in this process. Tests marked `subprocess` keep the real stdio
    subprocess (process lifecycle, launch env); any other test that leaves one running fails."""
    if request.node.get_closest_marker("subprocess") is not None:
        yield
        return
    monkeypatch.setattr(Agent, "_transport", lambda self: lambda: in_process_transport(self._server_settings()))
    yield
    assert mcp_children() == [], "an unmarked test started an MCP server process; mark it `subprocess`"


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
        **{"model": "test/model", **overrides},
    )


def query_limits() -> dict:
    """timeout_s and max_rows for calling queries.run_query directly."""
    cfg = Settings().agent_config()
    return {"timeout_s": cfg.query_timeout_s, "max_rows": cfg.max_result_rows}


@pytest.fixture
def build_agent(tmp_path, telemetry_db, semantic_layer_path):
    """Factory for an Agent that is NOT started (web tests: the app lifespan starts it)."""

    def _build(llm, **overrides) -> Agent:
        return Agent(config_for_test(tmp_path, telemetry_db, semantic_layer_path, **overrides), llm)

    return _build


@pytest.fixture
async def make_agent(build_agent):
    """Async factory: `agent = await make_agent(llm)`. Each Agent is started, and closed at teardown."""
    agents: list[Agent] = []

    async def _make(llm, **overrides) -> Agent:
        agent = build_agent(llm, **overrides)
        await agent.start()
        agents.append(agent)
        return agent

    yield _make
    for agent in agents:
        await agent.aclose()


@pytest.fixture
def web_client():
    """`web_client(agent)` -> a TestClient used as a context manager, so the app lifespan runs
    (starting the Agent's MCP server) and is left again at teardown."""
    with ExitStack() as stack:
        yield lambda agent, **kwargs: stack.enter_context(TestClient(create_app(agent, **kwargs)))


def mcp_children() -> list[int]:
    me, found = os.getpid(), []
    for pid in filter(str.isdigit, os.listdir("/proc")):
        try:
            with open(f"/proc/{pid}/stat") as f:
                ppid = int(f.read().rsplit(")", 1)[1].split()[1])
            if ppid != me:
                continue
            with open(f"/proc/{pid}/cmdline", "rb") as f:
                if b"flood_defender.mcp_server" in f.read():
                    found.append(int(pid))
        except (OSError, IndexError, ValueError):
            continue  # process vanished
    return found


async def collect(agent: Agent, message: str, conversation: Conversation | None = None):
    return [e async for e in agent.run_turn(conversation or Conversation(), message)]


__all__ = ["TextChunk"]
