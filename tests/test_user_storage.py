"""Each User's own Notebooks and tables: what one User makes, another cannot see or reach, over the same Telemetry
Database. Covers the web app, the MCP server's User, and what startup does to the databases."""

import json
import re

import pytest
from conftest import (
    DAY,
    SQL,
    TEST_PASSWORD_HASH,
    Clock,
    ScriptedLLM,
    add_sql_cell,
    ask,
    in_process_transport,
    last_tool_result,
    new_client,
    sign_in,
    sign_in_for_test,
    sql_rows,
    table_script,
    tool_call,
)
from mcp import ClientSession

from flood_agent.__main__ import build_app
from flood_agent.config import Settings
from flood_agent.llm import TextChunk, ToolCallChunk
from flood_agent.users import user_databases

ALICE, BOB = "alice", "bob"
BOTH = (ALICE, BOB)


@pytest.fixture
def llm():
    return ScriptedLLM()


@pytest.fixture
def agent(build_agent, llm):
    return build_agent(llm, users=BOTH)


@pytest.fixture
def alice(web_client, agent):
    client = web_client(agent, signed_in=False, sign_in_config=sign_in_for_test(users=BOTH))
    assert sign_in(client, ALICE).status_code == 303
    return client


@pytest.fixture
def bob(alice):
    """A second browser on the same running app."""
    client = new_client(alice.app)
    client.portal = alice.portal  # one event loop for the app, which the MCP connection lives on
    assert sign_in(client, BOB).status_code == 303
    return client


def new_notebook(client, title: str) -> str:
    notebook = client.post("/notebooks", follow_redirects=False).headers["location"].removeprefix("/n/")
    assert client.post(f"/n/{notebook}/rename", data={"title": title}, follow_redirects=False).status_code == 303
    return notebook


def run_sql(client, **draft) -> str:
    """Run a new SQL Cell holding `draft` in a new notebook of this browser's User; returns the finished cell's HTML."""
    notebook = new_notebook(client, "SQL")
    added = client.post(f"/n/{notebook}/cells", data={"kind": "sql"}).text
    cell = re.search(r'data-cell-id="(\w+)"', added).group(1)
    ran = client.post(f"/n/{notebook}/cells/{cell}/run", data={"max_rows": "100", **draft})
    assert ran.status_code == 200, ran.text
    client.get(f"/n/{notebook}/cells/{cell}/stream")
    return client.get(f"/n/{notebook}/cells/{cell}").text


def result_table(client, sql: str = "SELECT dport FROM flows") -> str:
    """The name of a new Result Table of `sql`, made by this browser's User."""
    return re.search(r"/api/results/(rt_\w+)/rows", run_sql(client, sql=sql)).group(1)


def add_lookup(client, name: str, rows: str) -> None:
    assert client.post("/api/lookup-tables", data={"name": name, "rows": rows}).status_code == 200


def listed(client) -> dict:
    listing = client.get("/api/tables").json()
    return {kind: listing[kind] for kind in ("lookup", "saved", "result")}


# --- separation --------------------------------------------------------------------------------------


def test_a_users_notebooks_are_listed_and_opened_by_that_user_alone(alice, bob):
    notebook = new_notebook(alice, "Alice's incident")

    assert [n["title"] for n in alice.get("/api/notebooks").json()] == ["Alice's incident"]
    assert bob.get("/api/notebooks").json() == []
    assert bob.get(f"/n/{notebook}").status_code == 404
    assert "Alice&#39;s incident" not in bob.get("/").text
    assert alice.get(f"/n/{notebook}").status_code == 200


def test_a_users_tables_are_listed_for_that_user_alone(alice, bob, agent):
    kept = result_table(alice)
    assert alice.post(f"/api/results/{kept}/settings", data={"name": "victims"}).status_code == 200
    made = result_table(alice)
    add_lookup(alice, "watch", "ip\n10.0.0.1")

    assert listed(alice) == {"lookup": ["lookup_watch"], "saved": ["victims"], "result": [made]}
    assert listed(bob) == {"lookup": [], "saved": [], "result": []}
    shown = run_sql(bob, command="SHOW TABLES")
    assert not [name for name in ("lookup_watch", "victims", made) if name in shown]
    assert "victims" in run_sql(alice, command="SHOW TABLES")
    assert agent.for_user(BOB).tables.readable() == set()
    for name in ("lookup_watch", "victims", made):
        assert agent.for_user(BOB).tables.kind_of(name) is None


def test_another_users_table_does_not_exist_over_http(alice, bob):
    table = result_table(alice)
    add_lookup(alice, "watch", "ip\n10.0.0.1")
    attempts = [
        *(("GET", f"/api/results/{table}/{what}", {}) for what in ("rows", "settings", "psv", "chart-rows")),
        ("POST", f"/api/results/{table}/settings", {"name": "mine"}),
        ("POST", f"/api/results/{table}/refresh", {}),
        ("GET", f"/api/tables/{table}/display", {}),
        ("GET", f"/api/tables/{table}/columns", {}),
        ("DELETE", f"/api/tables/{table}", {}),
        ("GET", "/api/lookup-tables/lookup_watch", {}),
        ("PUT", "/api/lookup-tables/lookup_watch", {"rows": "ip\n10.9.9.9"}),
        ("DELETE", "/api/tables/lookup_watch", {}),
    ]

    answers = {(m, path): bob.request(m, path, data=data or None).status_code for m, path, data in attempts}

    assert answers == dict.fromkeys(answers, 404)
    assert listed(alice) == {"lookup": ["lookup_watch"], "saved": [], "result": [table]}
    assert "10.0.0.1" in alice.get("/api/lookup-tables/lookup_watch").json()["text"]


def test_another_users_notebook_and_count_display_cannot_be_changed(alice, bob, agent):
    run_sql(alice, sql="SELECT COUNT(*) FROM flows")
    notebooks = agent.for_user(ALICE).notebooks
    notebook = notebooks.list_notebooks()[0]["id"]
    (cell,) = (c.cell.id for c in notebooks.notebook_page(notebook).cells if c.run)
    (count,) = (i for i in notebooks.run_transcript(notebook, cell) if i["type"] == "display")

    assert bob.post(f"/api/notebook-displays/{count['display_id']}/refresh").status_code == 404
    assert bob.get(f"/n/{notebook}/cells/{cell}").status_code == 404
    assert bob.get(f"/n/{notebook}/cells/{cell}/stream").status_code == 404
    assert bob.post(f"/n/{notebook}/delete", data={"tables": "keep"}).status_code == 404
    assert alice.get(f"/n/{notebook}").status_code == 200


def test_querying_another_users_table_finds_no_such_table(alice, bob):
    table = result_table(alice)

    refused = run_sql(bob, sql=f"SELECT * FROM {table}")

    assert f"no such table: {table}" in refused and "/api/results/" not in refused


def test_two_users_can_use_the_same_table_name(alice, bob):
    for client, port in ((alice, 8443), (bob, 9053)):
        add_lookup(client, "watch", f"port\n{port}")
        made = result_table(client, f"SELECT DISTINCT dport FROM flows WHERE dport = {port}")
        assert client.post(f"/api/results/{made}/settings", data={"name": "victims"}).status_code == 200

    for client, port in ((alice, 8443), (bob, 9053)):
        assert client.get("/api/results/victims/rows").json()["rows"] == [[port]]
        assert str(port) in client.get("/api/lookup-tables/lookup_watch").json()["text"]


def test_everyone_reads_the_same_telemetry_database(alice, bob):
    rows = [client.get(f"/api/results/{result_table(client, SQL)}/rows").json()["rows"] for client in (alice, bob)]

    assert rows[0] == rows[1] != []


# --- the User of an MCP tool call --------------------------------------------------------------------


def test_an_ai_cell_creates_its_result_table_in_the_signed_in_users_results_database(alice, bob, agent, llm):
    llm.steps = table_script()

    ask(bob, "top ports")

    (table,) = agent.for_user(BOB).tables.names()
    in_database = "SELECT name FROM sqlite_master WHERE name = ?"
    assert sql_rows(agent.for_user(BOB).databases.results_db, in_database, table)
    alices = agent.for_user(ALICE).databases.results_db
    assert not alices.exists() or not sql_rows(alices, in_database, table)
    assert listed(alice)["result"] == [] and bob.get(f"/api/results/{table}/rows").status_code == 200


def test_an_ai_cell_cannot_reach_another_users_table_even_by_naming_that_user(alice, bob, llm):
    table = result_table(alice)
    seen = []
    llm.steps = [
        [tool_call("run_query", "c1", sql=f"SELECT * FROM {table}", title="t", user=ALICE)],
        lambda m: seen.append(last_tool_result(m)) or [
            ToolCallChunk("c2", "describe_table", json.dumps({"name": table, "user": ALICE}))],
        lambda m: seen.append(last_tool_result(m)) or [tool_call("show_table", "c3", result_table=table)],
        lambda m: seen.append(last_tool_result(m)) or [TextChunk("done")],
    ]

    ask(bob, "show me alice's table")

    assert [result["success"] for result in seen] == [False, False, False]
    assert "no such table" in seen[0]["error"].lower()


@pytest.mark.parametrize("meta", [{}, {"flood_agent/user": "mallory"}, {"flood_agent/user": ""},
                                  {"flood_agent/user": ["alice"]}])
@pytest.mark.parametrize(
    "tool, arguments",
    [
        ("run_query", {"sql": "SELECT 1 AS a", "title": "t"}),
        ("run_count_query", {"sql": "SELECT COUNT(*) FROM flows", "title": "t"}),
        ("describe_table", {"name": "flows"}),
        ("show_table", {"result_table": "rt_x"}),
        ("show_chart", {"vega_lite_spec": {"mark": "bar", "data": {"name": "rt_x"}}}),
    ],
)
async def test_a_tool_call_without_a_known_user_is_refused(agent, tool, arguments, meta):
    async with in_process_transport(agent.server_settings()) as (r, w), ClientSession(r, w) as session:
        await session.initialize()
        result = await session.call_tool(tool, arguments, meta={"flood_agent/max_result_rows": 10, **meta})
        tools = (await session.list_tools()).tools

    assert result.isError and "known User" in result.content[0].text  # pyright: ignore[reportAttributeAccessIssue]
    assert "user" not in json.dumps([t.inputSchema["properties"] for t in tools]).lower()
    assert not [u for u in BOTH if agent.for_user(u).databases.results_db.exists()]


# --- startup -----------------------------------------------------------------------------------------


def app_settings(tmp_path, telemetry_db, semantic_layer_path) -> Settings:
    return Settings(  # pyright: ignore[reportCallIssue]
        users=",".join(f"{name}:{TEST_PASSWORD_HASH}" for name in BOTH), session_cookie_secure=False,
        telemetry_db=telemetry_db, semantic_layer=semantic_layer_path, users_dir=tmp_path / "people",
        app_db=tmp_path / "old" / "app.db", results_db=tmp_path / "old" / "results.db",
    )


def test_each_users_databases_are_created_on_first_use(tmp_path, telemetry_db, semantic_layer_path):
    settings = app_settings(tmp_path, telemetry_db, semantic_layer_path)

    with new_client(build_app(settings)) as client:
        sign_in(client, ALICE)
        assert all(user_databases(settings.users_dir, u).app_db.is_file() for u in BOTH)
        result_table(client)

    assert user_databases(settings.users_dir, ALICE).results_db.is_file()
    assert not settings.app_db.exists() and not settings.results_db.exists()


def test_startup_removes_the_single_user_databases_and_says_so(tmp_path, telemetry_db, semantic_layer_path, app_log):
    settings = app_settings(tmp_path, telemetry_db, semantic_layer_path)
    legacy = [settings.app_db, settings.results_db]
    legacy[0].parent.mkdir()
    for path in legacy:
        path.write_bytes(b"old data")
    build_app(settings)
    removed = [r.getMessage() for r in app_log.records]
    app_log.clear()
    build_app(settings)  # nothing left to remove
    again = [r.getMessage() for r in app_log.records]

    assert not any(path.exists() for path in legacy)
    assert all(any(str(path) in line for line in removed) for path in legacy)
    assert again == []


@pytest.mark.parametrize("database_left", [True, False], ids=["with the database", "left on its own"])
@pytest.mark.parametrize("suffix", ["-journal", "-wal", "-shm"])
def test_startup_removes_what_sqlite_kept_beside_the_single_user_databases(
    tmp_path, telemetry_db, semantic_layer_path, suffix, database_left
):
    settings = app_settings(tmp_path, telemetry_db, semantic_layer_path)
    legacy = [settings.app_db, settings.results_db]
    legacy[0].parent.mkdir()
    sidecars = [path.with_name(path.name + suffix) for path in legacy]
    for path in sidecars + (legacy if database_left else []):
        path.write_bytes(b"old data")

    build_app(settings)

    assert list(legacy[0].parent.iterdir()) == []


async def test_startup_cleanup_drops_every_users_expired_tables(build_agent):
    clock = Clock()
    agent = build_agent(ScriptedLLM(), users=BOTH, clock=clock, result_ttl_ms=DAY)
    for user in BOTH:
        notebooks = agent.for_user(user).notebooks
        notebook = notebooks.create_notebook()
        cell = add_sql_cell(agent.for_user(user), notebook, sql="SELECT dport FROM flows")
        async for _ in notebooks.run_cell(notebook, cell):
            pass

    def stored() -> list[int]:
        count = "SELECT COUNT(*) FROM sqlite_master WHERE name LIKE 'rt!_%' ESCAPE '!'"
        return [sql_rows(agent.for_user(u).databases.results_db, count)[0][0] for u in BOTH]

    assert stored() == [1, 1]
    clock.ms += 2 * DAY
    build_agent(ScriptedLLM(), users=BOTH, clock=clock, result_ttl_ms=DAY)
    assert stored() == [0, 0]
