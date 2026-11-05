"""Shared fixtures: tiny seeded Telemetry Database, results DB, scripted fake LLM."""

import copy
import functools
import hashlib
import json
import os
import shutil
import socket
import sqlite3
import sys
import threading
import time
from contextlib import ExitStack, asynccontextmanager
from dataclasses import replace
from pathlib import Path

import anyio
import pytest
import uvicorn
from fastapi.testclient import TestClient
from mcp.shared.memory import create_client_server_memory_streams
from playwright.sync_api import sync_playwright

from flood_agent.agent import Agent, AgentConfig, Conversation, TextDelta
from flood_agent.config import Settings
from flood_agent.llm import TextChunk, ToolCallChunk, UsageChunk
from flood_agent.mcp_server import ServerSettings, create_server
from flood_agent.web import create_app

# SQLite fsyncs on every commit, and on a disk filesystem (WSL's ext4 especially) that was over
# half the suite's time. Put pytest's temp root on tmpfs where there is one. Set at import, before
# any tmp_path is made, so xdist workers (which inherit the environment) use it too.
if os.access("/dev/shm", os.W_OK):
    os.environ.setdefault("PYTEST_DEBUG_TEMPROOT", "/dev/shm")

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


def pytest_addoption(parser):
    parser.addoption("--network", action="store_true", help="also run tests marked `network` (need internet access)")
    parser.addoption("--browser", action="store_true", help="also run tests marked `browser` (headless system Chrome)")


def pytest_collection_modifyitems(config, items):
    """Tests using `live_page` get the `browser` marker. `browser` and `network` tests are opt-in
    (--browser, --network) whatever -m expression is given, so the default run stays fast and offline."""
    for item in items:
        if "live_page" in getattr(item, "fixturenames", ()):
            item.add_marker(pytest.mark.browser)
    if not config.getoption("--browser"):
        skip_browser = pytest.mark.skip(reason="drives headless Chrome; run with --browser")
        for item in items:
            if "browser" in item.keywords:
                item.add_marker(skip_browser)
    if config.getoption("--network"):
        return
    skip = pytest.mark.skip(reason="needs internet access; run with --network")
    for item in items:
        if "network" in item.keywords:
            item.add_marker(skip)


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


DAY = 24 * 3_600_000  # ms


class Clock:
    """An injectable store clock (epoch ms) that tests move forward by setting `ms`."""

    def __init__(self, ms=1_000_000):
        self.ms = ms

    def __call__(self):
        return self.ms


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


SPAWN_EVENTS = {"subprocess.Popen", "os.posix_spawn", "os.fork", "os.exec"}
_spawns = [0]  # processes started in this worker so far


def _count_spawns(event: str, args) -> None:
    if event in SPAWN_EVENTS:
        _spawns[0] += 1


sys.addaudithook(_count_spawns)


@pytest.fixture(autouse=True)
def in_process_mcp_server(request, monkeypatch):
    """Agents run their MCP server in this process. Tests marked `subprocess` keep the real stdio
    subprocess (process lifecycle, launch env); any other test that leaves one running fails."""
    if request.node.get_closest_marker("subprocess") is not None:
        yield
        return
    monkeypatch.setattr(Agent, "_transport", lambda self: lambda: in_process_transport(self._server_settings()))
    spawns = _spawns[0]
    yield
    # Scanning /proc costs a few ms, so only after a test that started a process.
    assert _spawns[0] == spawns or mcp_children() == [], "an unmarked test started an MCP server process; mark it `subprocess`"


@pytest.fixture(autouse=True)
def isolated_settings(monkeypatch):
    """Settings must not pick up a developer's .env file or exported variables."""
    monkeypatch.setitem(Settings.model_config, "env_file", None)
    for name in Settings.model_fields:
        monkeypatch.delenv(name.upper(), raising=False)


@functools.cache
def default_agent_config() -> AgentConfig:
    """Built once per worker: constructing Settings costs about 3 ms. Callers get copies (`replace`)."""
    return Settings.from_defaults().agent_config()


def config_for_test(tmp_path, telemetry_db, semantic_layer, **overrides) -> AgentConfig:
    """The defaults.toml settings, pointed at this test's files."""
    return replace(
        default_agent_config(),
        telemetry_db=telemetry_db,
        results_db=tmp_path / "results.db",
        app_db=tmp_path / "app.db",
        semantic_layer=semantic_layer,
        **{"model": "test/model", **overrides},
    )


def query_limits() -> dict:
    """timeout_s and max_rows for calling queries.run_query directly."""
    cfg = default_agent_config()
    return {"timeout_s": cfg.query_timeout_s, "max_rows": 1_000_000}


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


@pytest.fixture
def live_server(build_agent):
    """`live_server(agent=None, **app_kwargs)` -> base URL of the real app served by uvicorn on a free
    port, for browser tests. Defaults to an Agent with a ScriptedLLM that has no steps."""
    servers: list[tuple[uvicorn.Server, threading.Thread]] = []

    def _start(agent: Agent | None = None, **app_kwargs) -> str:
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        app = create_app(agent or build_agent(ScriptedLLM()), **app_kwargs)
        server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
        thread = threading.Thread(target=server.run, daemon=True)
        thread.start()
        deadline = time.monotonic() + 15
        while not server.started:
            if not thread.is_alive() or time.monotonic() > deadline:
                raise RuntimeError("live_server did not start")
            time.sleep(0.02)
        servers.append((server, thread))
        return f"http://127.0.0.1:{port}"

    yield _start
    for server, thread in servers:
        server.should_exit = True
        thread.join(timeout=10)


CDN_HOSTS = ("https://unpkg.com/**", "https://cdn.jsdelivr.net/**")


@pytest.fixture(scope="module")
def chrome():
    """Headless system Chrome, shared by one test module's browser tests. Module scope, not session:
    sync Playwright keeps an event loop running in the worker while it is alive, which breaks async
    tests; pytest tears this down before the worker runs a test from another module. So keep async
    tests out of browser test modules."""
    playwright = sync_playwright().start()
    browser = playwright.chromium.launch(channel="chrome")
    yield browser
    browser.close()
    playwright.stop()


@pytest.fixture
def live_page(chrome, live_server, request):
    """`live_page(path="/", agent=None)` -> a fresh page (its own browser context) in headless system
    Chrome, opened on the live app. CDN scripts (htmx, Alpine, Vega) are served from pytest's cache
    after the first download, so browser tests run offline once warmed; with a cold cache and no
    internet they skip."""
    cache_dir = request.config.cache.mkdir("cdn")
    context = chrome.new_context()
    page = context.new_page()
    page.set_default_timeout(5000)  # the app is local: fail fast instead of Playwright's 30s

    def serve_cached(route):
        path = cache_dir / hashlib.sha256(route.request.url.encode()).hexdigest()
        if not path.exists():
            try:
                response = route.fetch()
            except Exception:
                route.abort()
                return
            if not response.ok:
                route.fulfill(response=response)
                return
            tmp = path.with_suffix(f".{os.getpid()}.tmp")
            tmp.write_bytes(response.body())
            os.replace(tmp, path)
        route.fulfill(path=path, content_type="application/javascript")

    for pattern in CDN_HOSTS:
        page.route(pattern, serve_cached)

    def _open(path: str = "/", agent: Agent | None = None):
        page.goto(live_server(agent) + path)
        if not page.evaluate("() => !!(window.Alpine && window.htmx)"):
            pytest.skip("CDN scripts unavailable (no internet and nothing cached yet)")
        return page

    yield _open
    context.close()


def mcp_children() -> list[int]:
    me, found = os.getpid(), []
    for pid in filter(str.isdigit, os.listdir("/proc")):
        try:
            with open(f"/proc/{pid}/stat") as f:
                ppid = int(f.read().rsplit(")", 1)[1].split()[1])
            if ppid != me:
                continue
            with open(f"/proc/{pid}/cmdline", "rb") as f:
                if b"flood_agent.mcp_server" in f.read():
                    found.append(int(pid))
        except (OSError, IndexError, ValueError):
            continue  # process vanished
    return found


async def collect(
    agent: Agent, message: str, conversation: Conversation | None = None, max_result_rows: int = 1_000_000
):
    return [e async for e in agent.run_turn(conversation or Conversation(), message, max_result_rows)]


def text_of(events) -> str:
    """All streamed assistant text in a turn's events, joined."""
    return "".join(e.text for e in events if isinstance(e, TextDelta))


__all__ = ["TextChunk"]
