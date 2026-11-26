"""Downloading a Result Table: what the options and PSV endpoints answer for a restricted and an unrestricted one,
and what a masked file reads back to (HTTP seam, ADR 0008)."""

import csv
import io
import re
import subprocess
import threading
import tracemalloc
from pathlib import Path

import pyrage
import pytest
import yaml
from conftest import ScriptedLLM, add_sql_cell
from test_public_key import ED25519, ED25519_FINGERPRINT

from flood_agent.catalog import Table
from flood_agent.downloads import check, psv_file
from flood_agent.lineage import Restriction
from flood_agent.public_keys import parse_public_key
from flood_agent.table_replacement import Rerun

pytestmark = pytest.mark.generated_data

REAL = Path(__file__).parent.parent / "semantic_layer.yaml"
RESTRICTED = "SELECT imsi, apn FROM diameter"
UNRESTRICTED = "SELECT apn FROM diameter"
UNKNOWN = 'SELECT apn, "no such column" AS text FROM diameter'  # runs in SQLite; lineage cannot resolve it


@pytest.fixture
def semantic_layer_path(tmp_path: Path) -> Path:
    """A copy of the shipped Semantic Layer, which has the Restricted Columns, for a test to change."""
    path = tmp_path / "semantic_layer.yaml"
    path.write_text(REAL.read_text())
    return path


@pytest.fixture
def agent(build_agent):
    return build_agent(ScriptedLLM())


@pytest.fixture
def client(web_client, agent):
    return web_client(agent)


def make_table(client, agent, sql: str) -> str:
    """Run `sql` in a new SQL Cell; returns its Result Table's name."""
    notebook = agent.notebooks.create_notebook()
    cell = add_sql_cell(agent, notebook, sql)
    assert client.post(f"/n/{notebook}/cells/{cell}/run", data={"sql": sql, "max_rows": "50"}).status_code == 200
    client.get(f"/n/{notebook}/cells/{cell}/stream")
    [table] = agent.tables.tables_of_cell(cell)
    return table.name


def options(client, table: str) -> dict:
    response = client.get(f"/api/results/{table}/download-options")
    assert response.status_code == 200
    return response.json()


@pytest.mark.parametrize("sql, expected", [
    (UNRESTRICTED, {"restricted": False, "restricted_columns": [], "lineage_unknown": False}),
    (RESTRICTED, {"restricted": True, "restricted_columns": ["imsi"], "lineage_unknown": False}),
])
def test_the_options_say_whether_a_table_is_restricted_and_by_which_columns(client, agent, sql, expected):
    assert options(client, make_table(client, agent, sql)) == {**expected, "public_key": None}


def test_the_options_name_the_type_and_fingerprint_of_the_public_key_on_file(client, agent):
    table = make_table(client, agent, RESTRICTED)
    add_key(client, ED25519)
    assert options(client, table)["public_key"] == {"type": "ssh-ed25519", "fingerprint": ED25519_FINGERPRINT}


def test_the_options_of_an_unknown_table_are_not_found(client):
    assert client.get("/api/results/rt_nope/download-options").status_code == 404


def psv(client, table: str, **params):
    return client.get(f"/api/results/{table}/psv", params=params)


def test_a_restricted_table_is_refused_plain_naming_its_columns_and_the_other_modes(client, agent):
    table = make_table(client, agent, "SELECT imsi, msisdn AS phone, apn FROM diameter")
    for params in ({}, {"mode": "plain"}):
        response = psv(client, table, **params)
        assert response.status_code == 403
        assert response.json()["detail"] == (
            f"{table} has Restricted Columns (imsi, phone), so it cannot be downloaded plain. "
            "Download it masked or encrypted.")


def test_a_table_of_unknown_lineage_is_refused_plain_and_masked(client, agent):
    table = make_table(client, agent, UNKNOWN)
    for mode in ("plain", "masked"):
        response = psv(client, table, mode=mode)
        assert response.status_code == 403
        assert response.json()["detail"] == (
            f"CANOPY couldn't tell which columns of {table} carry restricted values, "
            "so it can only be downloaded encrypted.")


# Two Restricted Columns and one that is not, with values chosen here (`WHERE 0` takes none from the database).
CHOSEN = (
    "SELECT imsi AS caller, msisdn AS callee, apn AS note FROM diameter WHERE 0 "
    "UNION ALL SELECT '001010000000001', '001010000000002', 'a|b' "
    "UNION ALL SELECT '001010000000002', '001010000000001', '=1+1' "
    "UNION ALL SELECT NULL, '', NULL "
    "UNION ALL SELECT '+999', '001010000000001', '001010000000001'"
)


def parsed(response) -> list[list[str]]:
    assert response.status_code == 200, response.text
    return list(csv.reader(io.StringIO(response.text, newline=""), delimiter="|"))


def test_a_masked_file_holds_each_restricted_values_token_and_everything_else_as_in_a_plain_one(
        client, agent, monkeypatch):
    table = make_table(client, agent, CHOSEN)
    monkeypatch.setattr("secrets.token_bytes", lambda n: bytes(range(n)))
    response = psv(client, table, mode="masked")
    assert response.headers["content-type"] == "text/plain; charset=utf-8"
    assert response.headers["content-disposition"] == f'attachment; filename="{table}.masked.psv"'
    # Each token is `openssl dgst -sha256 -mac HMAC` of the plain file's text with the 32-byte key 00..1f, cut to
    # its first 16 hex characters.
    one, two, formula = "2e2295720a5ab0a0", "acfeed6978c71986", "3588d25503ebcfb2"
    assert parsed(response) == [
        ["caller", "callee", "note"],
        [one, two, "a|b"],
        [two, one, "'=1+1"],
        ["", "", ""],
        [formula, one, "001010000000001"],
    ]


def test_a_second_masked_download_gives_different_tokens(client, agent):
    table = make_table(client, agent, CHOSEN)
    first, second = (parsed(psv(client, table, mode="masked")) for _ in range(2))
    assert re.fullmatch("[0-9a-f]{16}", first[1][0]) and re.fullmatch("[0-9a-f]{16}", second[1][0])
    assert first[1][0] != second[1][0]
    assert [row[2] for row in first] == [row[2] for row in second]


def add_key(client, public_key: str) -> None:
    """Put `public_key` on file, as the Settings page does."""
    response = client.post("/settings/public-key", data={"key": public_key}, follow_redirects=False)
    assert response.status_code == 303


def test_an_encrypted_file_decrypts_to_exactly_the_plain_one(client, agent):
    identity = pyrage.x25519.Identity.generate()
    add_key(client, str(identity.to_public()))
    table = make_table(client, agent, UNRESTRICTED)
    plain = psv(client, table).content
    response = psv(client, table, mode="encrypted")
    assert response.status_code == 200, response.text
    assert response.headers["content-type"] == "application/octet-stream"
    assert response.headers["content-disposition"] == f'attachment; filename="{table}.psv.age"'
    assert response.content.startswith(b"age-encryption.org/v1\n")  # binary format, not armored
    assert pyrage.decrypt(response.content, [identity]) == plain


@pytest.mark.parametrize("key_type", ["ed25519", "rsa"])
def test_an_encrypted_file_for_an_ssh_key_decrypts_with_that_ssh_identity(client, agent, tmp_path, key_type):
    private = tmp_path / "id"
    subprocess.run(["ssh-keygen", "-q", "-t", key_type, "-b", "2048", "-N", "", "-C", "me@laptop", "-f", private],
                   check=True)
    add_key(client, private.with_suffix(".pub").read_text())
    table = make_table(client, agent, RESTRICTED)
    response = psv(client, table, mode="encrypted")
    assert response.status_code == 200, response.text
    plain = pyrage.decrypt(response.content, [pyrage.ssh.Identity.from_buffer(private.read_bytes())])
    header, *rows = csv.reader(io.StringIO(plain.decode(), newline=""), delimiter="|")
    assert header == ["imsi", "apn"] and rows and all(re.fullmatch("[0-9]+", row[0]) for row in rows)  # real values


def test_an_encrypted_download_with_no_public_key_is_refused(client, agent):
    for sql in (UNRESTRICTED, RESTRICTED):
        response = psv(client, make_table(client, agent, sql), mode="encrypted")
        assert response.status_code == 409 and response.json()["detail"] == "No Public Key on file"
    assert psv(client, "rt_nope", mode="encrypted").status_code == 404


def test_a_table_of_unknown_lineage_downloads_encrypted(client, agent):
    identity = pyrage.x25519.Identity.generate()
    add_key(client, str(identity.to_public()))
    table = make_table(client, agent, UNKNOWN)
    response = psv(client, table, mode="encrypted")
    assert response.status_code == 200, response.text
    assert pyrage.decrypt(response.content, [identity]).startswith(b"apn|text\r\n")


def restrict_apn(path: Path) -> None:
    """Mark `diameter.apn` restricted too, as a later change to the Semantic Layer would."""
    layer = yaml.safe_load(path.read_text())
    layer["tables"]["diameter"]["columns"]["apn"]["restricted"] = True
    path.write_text(yaml.safe_dump(layer, sort_keys=False))


def test_a_newly_restricted_column_restricts_the_tables_already_made_from_it(
        build_agent, web_client, semantic_layer_path):
    first = build_agent(ScriptedLLM())
    client = web_client(first)
    saved = make_table(client, first, "SELECT apn, imsi, etime FROM diameter")
    assert client.post(f"/api/results/{saved}/settings", data={"name": "subscribers"}).status_code == 200
    plain = make_table(client, first, "SELECT apn AS network FROM diameter")
    chained = make_table(client, first, f"SELECT network AS name FROM {plain}")  # two tables from the source
    assert not options(client, chained)["restricted"]

    restrict_apn(semantic_layer_path)
    client = web_client(build_agent(ScriptedLLM()))  # the app restarted

    assert options(client, "subscribers")["restricted_columns"] == ["apn", "imsi"]
    assert options(client, plain)["restricted_columns"] == ["network"]
    assert options(client, chained)["restricted_columns"] == ["name"]
    assert psv(client, chained).status_code == 403
    header, *rows = parsed(psv(client, "subscribers", mode="masked"))
    assert rows and all(re.fullmatch("[0-9a-f]{16}", row[0]) for row in rows)


def test_a_table_never_downloads_less_restricted_than_it_was_recorded(build_agent, web_client, semantic_layer_path):
    first = build_agent(ScriptedLLM())
    client = web_client(first)
    known = make_table(client, first, RESTRICTED)
    unknown = make_table(client, first, UNKNOWN)
    source = make_table(client, first, "SELECT imsi AS id FROM diameter")
    orphan = make_table(client, first, f"SELECT id FROM {source}")
    first.tables.delete(source)  # its query can no longer be analysed

    layer = yaml.safe_load(semantic_layer_path.read_text())
    del layer["tables"]["diameter"]["columns"]["imsi"]["restricted"]
    semantic_layer_path.write_text(yaml.safe_dump(layer, sort_keys=False))
    client = web_client(build_agent(ScriptedLLM()))

    assert options(client, known)["restricted_columns"] == ["imsi"]
    assert options(client, orphan)["restricted_columns"] == ["id"]
    assert options(client, unknown)["lineage_unknown"]


def test_each_masked_or_encrypted_download_is_logged_and_nothing_else_is(client, agent, app_log):
    restricted = make_table(client, agent, "SELECT imsi, msisdn AS phone, apn FROM diameter")
    unrestricted = make_table(client, agent, UNRESTRICTED)
    app_log.set_level("INFO", "uvicorn.error")
    app_log.clear()

    def logged() -> list[str]:  # one per record: `app_log` may hear a record twice
        records = {id(r): r.getMessage() for r in app_log.records}
        return [message for message in records.values() if message.startswith("download")]

    psv(client, unrestricted)
    psv(client, restricted)  # refused
    psv(client, restricted, mode="encrypted")  # refused: no key yet
    assert logged() == []

    psv(client, restricted, mode="masked")
    masked = f'download: user=tester table={restricted} mode=masked restricted_columns=["imsi", "phone"]'
    assert logged() == [masked]

    add_key(client, ED25519)
    psv(client, restricted, mode="encrypted")
    assert logged() == [masked, f'download: user=tester table={restricted} mode=encrypted '
                                f'restricted_columns=["imsi", "phone"] key={ED25519_FINGERPRINT}']


def test_a_large_masked_file_is_made_as_it_is_sent():
    read = 0

    def rows():
        nonlocal read
        for n in range(100_000):
            read += 1
            yield f"00101{n:010}", "internet"

    table = Table("subscribers", "saved", "", [], "", None, 1, 1, restriction=Restriction(("imsi",)))
    chunks = psv_file(check(table, "masked"), ["imsi", "apn"], rows()).chunks
    assert len(next(chunks)) < 100_000 and read < 5_000  # the first piece, from the first few thousand rows
    assert sum(chunk.count("\n") for chunk in chunks) > 90_000


def test_a_large_encrypted_file_is_never_held_in_memory(tmp_path):
    identity = pyrage.x25519.Identity.generate()
    rows = ((f"{n:08}" + "x" * 1000,) for n in range(20_000))  # 20 MB, a row at a time
    table = Table("subscribers", "saved", "", [], "", None, 1, 1, restriction=Restriction(("imsi",)))
    sent = tmp_path / "subscribers.psv.age"
    tracemalloc.start()
    try:
        with sent.open("wb") as file:
            allowed = check(table, "encrypted", parse_public_key(str(identity.to_public())))
            for piece in psv_file(allowed, ["imsi"], rows).chunks:
                file.write(piece)
        peak = tracemalloc.get_traced_memory()[1]
    finally:
        tracemalloc.stop()
    assert peak < 5_000_000
    assert len(pyrage.decrypt(sent.read_bytes(), [identity])) == len("imsi\r\n") + 20_000 * 1010


def rerun(agent, table: str, sql: str) -> None:
    """Replace `table` with `sql`'s rows, as its SQL Cell's re-run does."""
    agent.replacement.replace(table, sql, 50, rerun=Rerun(agent.tables[table].owner, "T"))


def test_a_download_during_a_replacement_waits_for_the_new_rows_and_their_restriction(client, agent, monkeypatch):
    table = make_table(client, agent, "SELECT imsi AS v FROM diameter")
    swap, answers = agent.queries.swap, []
    download = threading.Thread(target=lambda: answers.append(psv(client, table)))

    def download_then_swap(temp: str, name: str):  # the Catalog now describes rows that are not in place yet
        download.start()
        download.join(0.5)
        swap(temp, name)

    monkeypatch.setattr(agent.queries, "swap", download_then_swap)
    rerun(agent, table, "SELECT 'new' AS v")
    download.join()
    assert parsed(answers[0]) == [["v"], ["new"]]


def test_a_download_that_has_started_keeps_the_rows_it_was_checked_for(client, agent):
    table = make_table(client, agent, "SELECT 'old' AS v")
    chunks = agent.user.download(table, "plain").chunks
    replacing = threading.Thread(target=rerun, args=(agent, table, "SELECT imsi AS v FROM diameter"))
    replacing.start()
    replacing.join(0.5)  # it cannot put its rows in place while the old ones are still being read
    assert "".join(chunks) == "v\r\nold\r\n"
    replacing.join()
    assert agent.tables[table].restricted


@pytest.mark.parametrize("change", ["rename", "delete"])
def test_no_table_can_be_renamed_or_deleted_between_a_downloads_check_and_its_rows(
        client, agent, monkeypatch, change):
    plain = make_table(client, agent, "SELECT 'old' AS v")
    agent.tables.update(plain, "shared")
    restricted = make_table(client, agent, "SELECT imsi AS v FROM diameter")

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
