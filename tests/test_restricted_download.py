"""Restricted Tables and their downloads (HTTP seam, ADR 0008): which tables are restricted, what the options and PSV
endpoints answer for them, and what an encrypted file holds.

The Telemetry Database here has the generated one's tables and indexes and no rows, so no query, however it joins them,
builds anything. Where a test needs rows it chooses them itself (`CHOSEN`)."""

import json
import sqlite3
import subprocess
import threading
import tracemalloc
from contextlib import closing
from pathlib import Path

import pyrage
import pytest
import yaml
from conftest import (
    DAY,
    Clock,
    ScriptedLLM,
    add_ai_cell,
    add_sql_cell,
    empty_copy,
    last_tool_result,
    register_table,
    tool_call,
)
from test_public_key import ED25519, ED25519_FINGERPRINT

from flood_agent.catalog import Restriction, Table
from flood_agent.downloads import check, psv_file
from flood_agent.llm import TextChunk, ToolCallChunk
from flood_agent.public_keys import parse_public_key
from flood_agent.table_replacement import Rerun

REAL = Path(__file__).parent.parent / "semantic_layer.yaml"
SUBSCRIBER_COLUMNS = ["imsi", "msisdn", "imei"]
DIAMETER = [{"table": "diameter", "restricted_columns": SUBSCRIBER_COLUMNS}]
RESTRICTED = "SELECT imsi, apn FROM diameter"
UNRESTRICTED = "SELECT dport FROM flows"
UNKNOWN = "SELECT value FROM json_each('[1]')"  # a table function: CANOPY cannot say where its rows come from
# Rows chosen here, from a table with Restricted Columns.
CHOSEN = (
    "SELECT imsi AS caller, apn AS note FROM diameter WHERE 0 "
    "UNION ALL SELECT '001010000000001', 'a|b' "
    "UNION ALL SELECT NULL, '=1+1'"
)
CHOSEN_FILE = b"caller|note\r\n001010000000001|\"a|b\"\r\n|'=1+1\r\n"


@pytest.fixture
def telemetry_db(tmp_path, generated_db) -> Path:
    return empty_copy(generated_db, tmp_path / "telemetry.db")


@pytest.fixture
def semantic_layer_path(tmp_path: Path) -> Path:
    """A copy of the shipped Semantic Layer, which has the Restricted Columns, for a test to change."""
    path = tmp_path / "semantic_layer.yaml"
    path.write_text(REAL.read_text())
    return path


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def llm():
    return ScriptedLLM()


@pytest.fixture
def agent(build_agent, llm, clock):
    return build_agent(llm, clock=clock)


@pytest.fixture
def client(web_client, agent):
    return web_client(agent)


def run_ai_cell(client, agent, prompt: str) -> None:
    notebook = agent.notebooks.create_notebook()
    cell = add_ai_cell(agent, notebook, prompt=prompt)
    run = client.post(f"/n/{notebook}/cells/{cell}/run", data={"prompt": prompt, "max_rows": "50"})
    assert run.status_code == 200, run.text
    client.get(f"/n/{notebook}/cells/{cell}/stream")


def run_cell(client, agent, sql: str, notebook: str | None = None, cell: str | None = None) -> str:
    """Run `sql` in a SQL Cell (a new one unless `cell` is given); returns its Result Table's name."""
    if cell is None:
        notebook = agent.notebooks.create_notebook()
        cell = add_sql_cell(agent, notebook, sql)
    assert client.post(f"/n/{notebook}/cells/{cell}/run", data={"sql": sql, "max_rows": "50"}).status_code == 200
    client.get(f"/n/{notebook}/cells/{cell}/stream")
    tables = agent.tables.tables_of_cell(cell)
    assert len(tables) == 1, agent.notebooks.page_cell(notebook, cell).run
    return tables[0].name


def options(client, table: str) -> dict:
    response = client.get(f"/api/results/{table}/download-options")
    assert response.status_code == 200, response.text
    return response.json()


def origin(client, table: str) -> tuple[bool, list, bool]:
    """What the options say of the table: whether it is restricted, by which sources, and whether any is unknown."""
    answer = options(client, table)
    return answer["restricted"], answer["sources"], answer["unknown_origin"]


BY_DIAMETER = (True, DIAMETER, False)
PLAIN = (False, [], False)
NOT_KNOWN = (True, [], True)


def psv(client, table: str, **params):
    return client.get(f"/api/results/{table}/psv", params=params)


def add_key(client, public_key: str) -> None:
    """Put `public_key` on file, as the Settings page does."""
    response = client.post("/settings/public-key", data={"key": public_key}, follow_redirects=False)
    assert response.status_code == 303


def new_identity(client) -> pyrage.x25519.Identity:
    identity = pyrage.x25519.Identity.generate()
    add_key(client, str(identity.to_public()))
    return identity


# --- which queries make a Restricted Table -------------------------------------------------------------------------


@pytest.mark.parametrize("sql", [
    RESTRICTED,
    "SELECT apn FROM diameter",  # no Restricted Column selected
    "SELECT command_code, COUNT(*) AS n FROM diameter GROUP BY command_code",
    "SELECT COUNT(*) + 0 AS n FROM diameter",  # a covering index is all that is opened
    "SELECT MAX(etime) AS latest FROM diameter",
    "SELECT 1 AS one FROM diameter WHERE 0",
    "SELECT sip FROM flows ORDER BY (SELECT MIN(imsi) FROM diameter)",
    "SELECT sip FROM flows GROUP BY sip HAVING sip > (SELECT MIN(apn) FROM diameter)",
    "SELECT f.sip FROM flows f JOIN diameter d ON d.ue_ip_address = f.sip",
    "SELECT f.sip FROM flows f LEFT JOIN diameter d ON d.ue_ip_address = f.sip",
    "SELECT COUNT(*) + 0 AS n FROM flows JOIN diameter USING (etime)",
    "SELECT flows.sip FROM flows NATURAL JOIN diameter",
    "SELECT COUNT(*) + 0 AS n FROM flows LEFT JOIN diameter USING (stime, etime)",
    "SELECT COUNT(*) + 0 AS n FROM flows RIGHT JOIN diameter USING (etime)",
    "SELECT COUNT(*) + 0 AS n FROM flows FULL JOIN diameter USING (etime)",
    "SELECT f.sip FROM flows f, diameter",
    "SELECT n FROM (SELECT COUNT(*) AS n FROM diameter)",
    "SELECT (SELECT apn FROM diameter LIMIT 1) AS apn",
    "SELECT sip FROM flows LIMIT (SELECT COUNT(*) FROM diameter)",
    "WITH d AS (SELECT * FROM diameter) SELECT COUNT(*) + 0 AS n FROM d",
    "WITH d AS MATERIALIZED (SELECT apn FROM diameter) SELECT 1 AS one FROM d",
    "WITH RECURSIVE n(i) AS (SELECT 1 UNION ALL SELECT i + 1 FROM n, diameter WHERE i < 3) SELECT i FROM n",
    "SELECT sip AS v FROM flows UNION SELECT apn FROM diameter",
    "SELECT sip AS v FROM flows UNION ALL SELECT apn FROM diameter",
    "SELECT sip AS v FROM flows INTERSECT SELECT ue_ip_address FROM diameter",
    "SELECT sip AS v FROM flows EXCEPT SELECT ue_ip_address FROM diameter",
    "SELECT sip FROM flows WHERE EXISTS (SELECT 1 FROM diameter)",
    "SELECT sip FROM flows WHERE NOT EXISTS (SELECT 1 FROM diameter WHERE ue_ip_address = flows.sip)",
    "SELECT sip FROM flows WHERE sip IN (SELECT ue_ip_address FROM diameter)",
    "SELECT CASE WHEN sip IN (SELECT imsi FROM diameter) THEN 1 ELSE 0 END AS known FROM flows",
    "SELECT row_number() OVER (ORDER BY apn) AS n FROM diameter",
    'SELECT COUNT(*) + 0 AS n FROM MAIN."DIAMETER"',
    "SELECT COUNT(*) + 0 AS n FROM [Diameter] AS flows",  # an alias that is another table's name
    "SELECT /* flows */ apn FROM `diameter` -- FROM flows",
])
def test_a_query_that_opens_a_table_with_restricted_columns_makes_a_restricted_table(client, agent, sql):
    table = run_cell(client, agent, sql)
    assert origin(client, table) == BY_DIAMETER
    assert psv(client, table).status_code == 403


@pytest.mark.parametrize("sql", [
    UNRESTRICTED,
    "SELECT f.sip, d.question FROM flows f JOIN dns_metadata d USING (sip, dip)",
    "SELECT 1 AS imsi",
    "SELECT 'diameter' AS diameter FROM flows",
    "WITH RECURSIVE n(i) AS (SELECT 1 UNION ALL SELECT i + 1 FROM n WHERE i < 3) SELECT i FROM n",
    "SELECT ip FROM lookup_watched",
    "SELECT f.sip FROM flows f JOIN lookup_watched w ON w.ip = f.sip",
])
def test_a_table_from_unrestricted_tables_lookup_tables_or_none_downloads_plain_at_once(client, agent, sql):
    agent.tables.add_lookup("watched", "ip\n10.0.0.1")
    table = run_cell(client, agent, sql)
    assert origin(client, table) == PLAIN
    response = psv(client, table)
    assert response.status_code == 200
    assert response.headers["content-disposition"] == f'attachment; filename="{table}.psv"'


@pytest.mark.parametrize("sql", [
    UNKNOWN,
    "SELECT COUNT(*) + 0 AS n FROM json_each('[1]')",
    "SELECT d.value FROM flows, json_each('[1]') AS d",
    "SELECT name FROM sqlite_master",
    "SELECT COUNT(*) + 0 AS n FROM sqlite_schema",
    "SELECT COUNT(*) + 0 AS n FROM sqlite_temp_master",
    "SELECT sip FROM flows WHERE EXISTS (SELECT 1 FROM results.sqlite_master)",
])
def test_a_query_that_opens_something_unrecognised_makes_a_table_of_unknown_origin(client, agent, sql):
    table = run_cell(client, agent, sql)
    assert origin(client, table) == NOT_KNOWN
    assert psv(client, table).status_code == 403


def test_a_lookup_table_joined_to_a_restricted_table_does_not_launder_it(client, agent):
    agent.tables.add_lookup("watched", "ip\n10.0.0.1")
    table = run_cell(client, agent, "SELECT w.ip FROM lookup_watched w JOIN diameter d ON d.ue_ip_address = w.ip")
    assert origin(client, table) == BY_DIAMETER


# --- the record follows the data -----------------------------------------------------------------------------------


def test_a_derived_table_stays_restricted_after_its_source_is_deleted_or_expires(client, agent, clock):
    source = run_cell(client, agent, "SELECT apn FROM diameter")
    derived = run_cell(client, agent, f"SELECT COUNT(*) + 0 AS n FROM {source}")
    chained = run_cell(client, agent, f"SELECT n, dport FROM {derived}, flows")
    agent.tables.update(chained, "kept")
    assert origin(client, "kept") == BY_DIAMETER

    agent.tables.delete(source)
    assert origin(client, derived) == BY_DIAMETER
    clock.ms += 2 * DAY  # `derived` expires; the Saved Table remains
    agent.tables.sweep()
    assert origin(client, "kept") == BY_DIAMETER
    assert origin(client, run_cell(client, agent, "SELECT n FROM kept")) == BY_DIAMETER


def test_renaming_and_saving_leave_restriction_unchanged(client, agent):
    for sql, expected in ((RESTRICTED, BY_DIAMETER), (UNRESTRICTED, PLAIN), (UNKNOWN, NOT_KNOWN)):
        table = run_cell(client, agent, sql)
        name = f"saved_{len(agent.tables.names())}"
        assert client.post(f"/api/results/{table}/settings", data={"name": name}).status_code == 200
        assert origin(client, name) == expected


def test_a_table_with_no_record_is_restricted_and_so_is_one_made_from_it(client, agent):
    old = run_cell(client, agent, UNRESTRICTED)
    with_no_record = agent.tables[old]
    agent.tables.record_sources(old, None)  # as a table made before sources were recorded
    assert with_no_record.sql == UNRESTRICTED  # its query is there, and is not read again
    assert origin(client, old) == NOT_KNOWN
    assert origin(client, run_cell(client, agent, f"SELECT dport FROM {old}")) == NOT_KNOWN


def test_an_ai_cell_query_records_its_sources_without_telling_the_llm(client, agent, llm):
    llm.steps = [[tool_call("run_query", sql="SELECT apn FROM diameter", title="t")], [TextChunk("ok")]]
    run_ai_cell(client, agent, "networks")
    name = last_tool_result(llm.calls[-1])["result_table"]
    assert origin(client, name) == BY_DIAMETER
    assert set(last_tool_result(llm.calls[-1])) == {"success", "result_table", "columns", "row_count"}


def test_the_llm_is_not_told_of_restricted_columns_in_a_result_table(client, agent, llm):
    table = run_cell(client, agent, RESTRICTED)
    call = ToolCallChunk(id="c1", name="describe_table", arguments=json.dumps({"name": table}))
    llm.steps = [[call], [TextChunk("ok")]]
    run_ai_cell(client, agent, "describe it")
    assert last_tool_result(llm.calls[-1])["columns"] == [
        {"name": "imsi", "type": "TEXT"}, {"name": "apn", "type": "TEXT"}]


def rerun(agent, table: str, sql: str) -> None:
    """Replace `table` with `sql`'s rows, as its SQL Cell's re-run does."""
    agent.replacement.replace(table, sql, 50, rerun=Rerun(agent.tables[table].owner, "T"))


def test_a_sql_cell_re_run_and_a_refresh_record_the_new_rows_sources(client, agent):
    notebook = agent.notebooks.create_notebook()
    cell = add_sql_cell(agent, notebook, "SELECT apn AS v FROM flows, diameter")
    source = run_cell(client, agent, "SELECT dport AS v FROM flows", notebook, cell)
    derived = run_cell(client, agent, f"SELECT v FROM {source}")
    assert origin(client, derived) == PLAIN

    assert run_cell(client, agent, "SELECT apn AS v FROM diameter", notebook, cell) == source
    assert origin(client, source) == BY_DIAMETER
    assert origin(client, derived) == PLAIN  # its rows are still the old ones

    assert client.post(f"/api/results/{derived}/refresh").status_code == 200
    assert origin(client, derived) == BY_DIAMETER

    assert run_cell(client, agent, "SELECT dport AS v FROM flows", notebook, cell) == source
    assert origin(client, source) == PLAIN
    assert client.post(f"/api/results/{derived}/refresh").status_code == 200
    assert origin(client, derived) == PLAIN


# --- a name the registry holds is never given to new rows ----------------------------------------------------------


@pytest.fixture
def one_second(monkeypatch):
    """Every query runs within the same second, so each is offered the same generated name first."""
    monkeypatch.setattr("flood_agent.queries._timestamp", lambda: "20260101_000000")


def fail_registry_writes(agent, column: str) -> None:
    """Make every later write of `column` in the table registry fail."""
    with closing(sqlite3.connect(agent.databases.app_db)) as con, con:
        con.execute(f"CREATE TRIGGER fail_write BEFORE UPDATE OF {column} ON result_tables "
                    "BEGIN SELECT RAISE(ABORT, 'disk full'); END")


def test_a_dropped_tables_name_is_not_given_to_a_later_query(client, agent, llm, one_second):
    dropped = run_cell(client, agent, UNRESTRICTED)
    agent.tables.delete(dropped)
    llm.steps = [[tool_call("run_query", sql="SELECT apn FROM diameter", title="t")], [TextChunk("ok")]]
    run_ai_cell(client, agent, "networks")
    name = last_tool_result(llm.calls[-1])["result_table"]
    assert name != dropped
    assert origin(client, name) == BY_DIAMETER


def test_a_name_stays_taken_when_its_rows_are_dropped_but_it_cannot_be_marked_expired(client, agent, one_second):
    source = run_cell(client, agent, UNRESTRICTED)
    fail_registry_writes(agent, "expired")
    with pytest.raises(sqlite3.IntegrityError):
        agent.tables.delete(source)
    new = run_cell_directly(agent, RESTRICTED)
    assert new != source
    assert origin(client, new) == BY_DIAMETER
    assert psv(client, source).status_code == 404


def test_a_query_cannot_take_the_old_name_of_a_table_being_renamed(client, agent, monkeypatch, one_second):
    source = run_cell(client, agent, UNRESTRICTED)
    fail_registry_writes(agent, "name")
    rename, made = agent.queries.rename, []

    def rename_then_query(old: str, new: str):  # the rows have moved; the registry still holds the old name
        rename(old, new)
        if not made:
            made.append(run_cell_directly(agent, RESTRICTED))

    monkeypatch.setattr(agent.queries, "rename", rename_then_query)
    with pytest.raises(ValueError):
        agent.tables.update(source, "kept")
    assert made[0] != source
    assert origin(client, made[0]) == BY_DIAMETER
    assert origin(client, source) == PLAIN and psv(client, source).status_code == 200  # the rename was undone


def test_a_query_cannot_take_the_name_of_a_table_being_deleted(client, agent, monkeypatch, one_second):
    source = run_cell(client, agent, UNRESTRICTED)
    drop, made = agent.queries.drop, []

    def drop_then_query(name: str):  # the rows are gone; the registry does not yet say the table is
        drop(name)
        made.append(run_cell_directly(agent, RESTRICTED))
        made.append(run_cell_directly(agent, f"SELECT apn FROM {made[0]}"))

    monkeypatch.setattr(agent.queries, "drop", drop_then_query)
    agent.tables.delete(source)
    assert source not in made
    assert [origin(client, name) for name in made] == [BY_DIAMETER, BY_DIAMETER]


# --- the configuration as it is now ---------------------------------------------------------------------------------


def reconfigured(build_agent, web_client, clock, path: Path, change):
    """A client of the app restarted after `change` to the Semantic Layer's tables."""
    layer = yaml.safe_load(path.read_text())
    change(layer["tables"])
    path.write_text(yaml.safe_dump(layer, sort_keys=False))
    return web_client(build_agent(ScriptedLLM(), clock=clock))


def test_marking_a_column_restricts_the_tables_already_made_from_its_table(
        build_agent, web_client, clock, client, agent, semantic_layer_path):
    table = run_cell(client, agent, UNRESTRICTED)
    chained = run_cell(client, agent, f"SELECT dport FROM {table}")

    def mark(tables):
        tables["flows"]["columns"]["sip"]["restricted"] = True

    client = reconfigured(build_agent, web_client, clock, semantic_layer_path, mark)
    for name in (table, chained):
        assert origin(client, name) == (True, [{"table": "flows", "restricted_columns": ["sip"]}], False)
        assert psv(client, name).status_code == 403


def test_unmarking_the_last_restricted_column_lets_existing_tables_download_plain(
        build_agent, web_client, clock, client, agent, semantic_layer_path):
    table = run_cell(client, agent, RESTRICTED)
    unknown = run_cell(client, agent, UNKNOWN)

    def unmark(tables):
        for column in SUBSCRIBER_COLUMNS:
            del tables["diameter"]["columns"][column]["restricted"]

    client = reconfigured(build_agent, web_client, clock, semantic_layer_path, unmark)
    assert origin(client, table) == PLAIN
    assert psv(client, table).status_code == 200
    assert origin(client, unknown) == NOT_KNOWN


def test_a_recorded_table_that_is_no_longer_described_is_restricted(
        build_agent, web_client, clock, client, agent, semantic_layer_path):
    table = run_cell(client, agent, "SELECT question FROM dns_metadata")
    both = run_cell(client, agent, "SELECT question FROM dns_metadata, diameter")

    client = reconfigured(build_agent, web_client, clock, semantic_layer_path, lambda tables: tables.pop("dns_metadata"))
    assert origin(client, table) == NOT_KNOWN
    assert origin(client, both) == (True, DIAMETER, True)
    response = psv(client, table)
    assert response.status_code == 403
    assert "CANOPY doesn't know where its data came from" in response.json()["detail"]


# --- downloads ------------------------------------------------------------------------------------------------------


def test_the_options_name_the_type_and_fingerprint_of_the_public_key_on_file(client, agent):
    table = run_cell(client, agent, RESTRICTED)
    assert options(client, table)["public_key"] is None
    add_key(client, ED25519)
    assert options(client, table)["public_key"] == {"type": "ssh-ed25519", "fingerprint": ED25519_FINGERPRINT}


def test_the_options_of_an_unknown_table_are_not_found(client):
    assert client.get("/api/results/rt_nope/download-options").status_code == 404


def test_a_restricted_table_is_refused_plain_with_the_reason(client, agent):
    restricted, unknown = run_cell(client, agent, RESTRICTED), run_cell(client, agent, UNKNOWN)
    add_key(client, ED25519)
    for params in ({}, {"mode": "plain"}):
        response = psv(client, restricted, **params)
        assert response.status_code == 403
        assert response.json()["detail"] == (
            f"{restricted} is a Restricted Table (it has data from diameter, which holds Restricted Columns "
            "(imsi, msisdn, imei)), so it can only be downloaded encrypted.")
        response = psv(client, unknown, **params)
        assert response.status_code == 403
        assert response.json()["detail"] == (
            f"{unknown} is a Restricted Table (CANOPY doesn't know where its data came from), "
            "so it can only be downloaded encrypted.")


@pytest.mark.parametrize("sql", [RESTRICTED, UNRESTRICTED])
def test_the_masked_mode_is_rejected_as_invalid(client, agent, sql):
    table = run_cell(client, agent, sql)
    add_key(client, ED25519)
    for mode in ("masked", "PLAIN", ""):
        assert psv(client, table, mode=mode).status_code == 422


@pytest.mark.parametrize("sql", [CHOSEN, "SELECT 'new' AS v"])
def test_an_encrypted_file_decrypts_to_exactly_what_a_plain_one_holds(client, agent, sql):
    identity = new_identity(client)
    table = run_cell(client, agent, sql)
    response = psv(client, table, mode="encrypted")
    assert response.status_code == 200, response.text
    assert response.headers["content-type"] == "application/octet-stream"
    assert response.headers["content-disposition"] == f'attachment; filename="{table}.psv.age"'
    assert response.content.startswith(b"age-encryption.org/v1\n")  # binary format, not armored
    assert pyrage.decrypt(response.content, [identity]) == (CHOSEN_FILE if sql == CHOSEN else b"v\r\nnew\r\n")


def test_an_unrestricted_tables_encrypted_file_is_its_plain_one(client, agent):
    identity = new_identity(client)
    table = run_cell(client, agent, "SELECT 7 AS n")
    response = psv(client, table, mode="encrypted")
    assert pyrage.decrypt(response.content, [identity]) == psv(client, table).content == b"n\r\n7\r\n"


@pytest.mark.parametrize("key_type", ["ed25519", "rsa"])
def test_an_encrypted_file_for_an_ssh_key_decrypts_with_that_ssh_identity(client, agent, tmp_path, key_type):
    private = tmp_path / "id"
    subprocess.run(["ssh-keygen", "-q", "-t", key_type, "-b", "2048", "-N", "", "-C", "me@laptop", "-f", private],
                   check=True)
    add_key(client, private.with_suffix(".pub").read_text())
    table = run_cell(client, agent, CHOSEN)
    response = psv(client, table, mode="encrypted")
    assert response.status_code == 200, response.text
    identity = pyrage.ssh.Identity.from_buffer(private.read_bytes())
    assert pyrage.decrypt(response.content, [identity]) == CHOSEN_FILE


def test_an_encrypted_download_with_no_public_key_is_refused(client, agent):
    for sql in (UNRESTRICTED, RESTRICTED):
        response = psv(client, run_cell(client, agent, sql), mode="encrypted")
        assert response.status_code == 409 and response.json()["detail"] == "No Public Key on file"
    assert psv(client, "rt_nope", mode="encrypted").status_code == 404


def test_each_encrypted_download_of_a_restricted_table_is_logged_and_nothing_else_is(client, agent, app_log):
    restricted = run_cell(client, agent, "SELECT f.sip FROM flows f, diameter")
    unknown = run_cell(client, agent, UNKNOWN)
    unrestricted = run_cell(client, agent, UNRESTRICTED)
    app_log.set_level("INFO", "uvicorn.error")
    app_log.clear()

    def logged() -> list[str]:  # one per record: `app_log` may hear a record twice
        records = {id(r): r.getMessage() for r in app_log.records}
        return [message for message in records.values() if message.startswith("download")]

    psv(client, unrestricted)
    psv(client, restricted)  # refused
    psv(client, restricted, mode="encrypted")  # refused: no key yet
    add_key(client, ED25519)
    psv(client, unrestricted, mode="encrypted")
    assert logged() == []

    psv(client, restricted, mode="encrypted")
    psv(client, unknown, mode="encrypted")
    assert logged() == [
        f'download: user=tester table={restricted} sources=["diameter", "flows"] key={ED25519_FINGERPRINT}',
        f'download: user=tester table={unknown} sources=unknown key={ED25519_FINGERPRINT}']


def test_a_large_encrypted_file_is_never_held_in_memory(tmp_path):
    identity = pyrage.x25519.Identity.generate()
    rows = ((f"{n:08}" + "x" * 1000,) for n in range(20_000))  # 20 MB, a row at a time
    table = Table("subscribers", "saved", "", [], "", None, 1, 1)
    sent = tmp_path / "subscribers.psv.age"
    tracemalloc.start()
    try:
        with sent.open("wb") as file:
            allowed = check(table, Restriction(unknown_origin=True), "encrypted",
                            parse_public_key(str(identity.to_public())))
            for piece in psv_file(allowed, ["imsi"], rows).chunks:
                file.write(piece)
        peak = tracemalloc.get_traced_memory()[1]
    finally:
        tracemalloc.stop()
    assert peak < 5_000_000
    assert len(pyrage.decrypt(sent.read_bytes(), [identity])) == len("imsi\r\n") + 20_000 * 1010


# --- downloads and queries during a replacement ---------------------------------------------------------------------


@pytest.mark.parametrize("old, new", [
    ("SELECT apn AS v FROM diameter", "SELECT 'new' AS v"),
    ("SELECT 'old' AS v", "SELECT apn AS v FROM diameter"),
])
def test_while_a_table_is_replaced_its_record_covers_both_the_old_rows_and_the_new(
        client, agent, monkeypatch, old, new):
    table = run_cell(client, agent, old)
    swap, during, derived = agent.queries.swap, [], []

    def look_then_swap(temp: str, name: str):  # the Catalog has been written; the old rows are still in place
        during.append(origin(client, table))
        derived.append(agent.queries.run(f"SELECT v FROM {table}", 50))
        swap(temp, name)
        during.append(origin(client, table))

    monkeypatch.setattr(agent.queries, "swap", look_then_swap)
    rerun(agent, table, new)
    assert during == [BY_DIAMETER, BY_DIAMETER]
    assert derived[0]["sources"] == {"diameter"}


def test_a_replacement_between_unrestricted_and_unknown_rows_is_unknown_while_it_swaps(client, agent, monkeypatch):
    table = run_cell(client, agent, UNKNOWN)
    swap, during = agent.queries.swap, []

    def look_then_swap(temp: str, name: str):
        during.append(origin(client, table))
        swap(temp, name)

    monkeypatch.setattr(agent.queries, "swap", look_then_swap)
    rerun(agent, table, "SELECT 1 AS value")
    assert during == [NOT_KNOWN]
    assert origin(client, table) == PLAIN


def test_a_failed_replacement_leaves_the_record_as_it_was(client, agent, monkeypatch):
    table = run_cell(client, agent, "SELECT 'old' AS v")

    def fail(temp: str, name: str):
        raise RuntimeError("no swap")

    monkeypatch.setattr(agent.queries, "swap", fail)
    with pytest.raises(RuntimeError, match="no swap"):
        rerun(agent, table, RESTRICTED)
    assert origin(client, table) == PLAIN


def test_a_replacement_whose_last_write_fails_stays_restricted(client, agent, monkeypatch):
    table = run_cell(client, agent, "SELECT apn AS v FROM diameter")

    def fail(name, sources):
        raise RuntimeError("disk full")

    monkeypatch.setattr(agent.tables, "record_sources", fail)
    with pytest.raises(RuntimeError, match="disk full"):
        rerun(agent, table, "SELECT 'new' AS v")
    assert origin(client, table) == BY_DIAMETER


def test_a_source_cannot_be_replaced_between_a_query_reading_it_and_its_record(agent, client, query_runner):
    source = run_cell(client, agent, "SELECT apn AS v FROM diameter")
    replacing = threading.Thread(target=rerun, args=(agent, source, "SELECT 'new' AS v"))

    def recorded(name: str):  # the sources are being established: the rows have been read
        if replacing.ident is None:
            replacing.start()
            replacing.join(0.5)
        return agent.tables.recorded_sources(name)

    result = query_runner(agent.tables.readable, recorded=recorded).run(f"SELECT v FROM {source}", 50)
    replacing.join()
    assert result["sources"] == {"diameter"}
    assert origin(client, source) == PLAIN


def test_a_download_during_a_replacement_waits_for_the_new_rows_and_their_record(client, agent, monkeypatch):
    table = run_cell(client, agent, CHOSEN)
    swap, answers = agent.queries.swap, []
    download = threading.Thread(target=lambda: answers.append(psv(client, table)))

    def download_then_swap(temp: str, name: str):  # the Catalog now describes rows that are not in place yet
        download.start()
        download.join(0.5)
        swap(temp, name)

    monkeypatch.setattr(agent.queries, "swap", download_then_swap)
    rerun(agent, table, "SELECT 'new' AS v")
    download.join()
    assert answers[0].status_code == 200 and answers[0].content == b"v\r\nnew\r\n"


def test_a_download_that_has_started_keeps_the_rows_it_was_checked_for(client, agent):
    table = run_cell(client, agent, "SELECT 'old' AS v")
    chunks = agent.user.download(table, "plain").chunks
    replacing = threading.Thread(target=rerun, args=(agent, table, RESTRICTED))
    replacing.start()
    replacing.join(0.5)  # it cannot put its rows in place while the old ones are still being read
    assert "".join(chunks) == "v\r\nold\r\n"
    replacing.join()
    assert origin(client, table) == BY_DIAMETER


@pytest.mark.parametrize("change", ["rename", "delete"])
def test_no_table_can_be_renamed_or_deleted_between_a_downloads_check_and_its_rows(
        client, agent, monkeypatch, change):
    plain = run_cell(client, agent, "SELECT 'old' AS v")
    agent.tables.update(plain, "shared")
    restricted = run_cell(client, agent, CHOSEN)

    def swap_in_the_restricted_table():
        if change == "delete":
            agent.tables.delete("shared")
        else:
            agent.tables.update("shared", "elsewhere")
            agent.tables.update(restricted, "shared")

    all_rows, changing = agent.queries.all_rows, threading.Thread(target=swap_in_the_restricted_table)

    def change_then_read(name: str):  # the download has been allowed; its rows are not yet being read
        changing.start()
        changing.join(0.5)
        return all_rows(name)

    monkeypatch.setattr(agent.queries, "all_rows", change_then_read)
    chunks = agent.user.download("shared", "plain").chunks
    assert "".join(chunks) == "v\r\nold\r\n"
    changing.join()


# --- every query CANOPY ships ----------------------------------------------------------------------------------------


def test_every_shipped_query_that_opens_diameter_comes_out_restricted_and_the_others_plain(client, agent):
    agent.tables.add_lookup("watchlist", "ip\n10.0.0.1")
    layer = yaml.safe_load(REAL.read_text())
    # (what it is, its SQL); a metric that is only an expression opens no table
    shipped = [(name, m["sql"].replace("<start>", "0").replace("<end>", "1"))
               for name, m in layer["metrics"].items() if m["sql"].startswith(("SELECT", "WITH"))]
    prompts = yaml.safe_load((REAL.parent / "src/flood_agent/canned_prompts.yaml").read_text())
    shipped += [(prompt["id"], call["args"]["sql"])
                for prompt in prompts if not {"failed_tool_calls", "notice"} & prompt["expect"].keys()
                for step in prompt["steps"] for call in step.get("tool_calls", []) if call["name"] == "run_query"]
    restricted, plain, made = set(), set(), ""
    for name, sql in shipped:
        made = run_cell_directly(agent, sql.replace("{result_table}", made))
        (restricted if origin(client, made) == BY_DIAMETER else plain).add(name)
        assert origin(client, made) in (BY_DIAMETER, PLAIN), name
    assert restricted == {
        "failure_rate", "timeouts_per_minute", "flows_subscriber_join",
        *(prompt["id"] for prompt in prompts if prompt["id"].startswith("diameter-"))}
    assert {"top_talkers", "flows_dns_join", "top-destinations"} <= plain


def run_cell_directly(agent, sql: str) -> str:
    """Make a Result Table from `sql`, LIMIT and all, through the query runner both Cell kinds use."""
    result = agent.queries.run(sql, 10)
    assert result["success"], result.get("raw_error")
    register_table(agent.tables, result["result_table"], sources=result["sources"])
    return result["result_table"]
