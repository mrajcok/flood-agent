"""The dev-only endpoint and button that shift the Telemetry Database's timestamps to now."""

import sqlite3

from conftest import ScriptedLLM
from playwright.sync_api import expect

NOW = 1_800_000_000_000


def latest(db):
    con = sqlite3.connect(db)
    try:
        return con.execute("SELECT MAX(etime) FROM flows").fetchone()[0]
    finally:
        con.close()


def test_the_endpoint_exists_only_in_dev_mode(build_agent, web_client):
    client = web_client(build_agent(ScriptedLLM()))
    assert client.post("/api/dev/shift-timestamps").status_code == 404
    assert "Shift data to now" not in client.get("/").text


def test_in_dev_mode_the_endpoint_shifts_the_data_to_the_agents_clock(build_agent, web_client, telemetry_db):
    client = web_client(build_agent(ScriptedLLM(), clock=lambda: NOW), dev=True)
    assert "Shift data to now" in client.get("/").text
    r = client.post("/api/dev/shift-timestamps")
    assert r.status_code == 200 and r.json()["delta_ms"] == NOW - 1_700_000_002_999
    assert latest(telemetry_db) == NOW


def test_the_button_shifts_the_data_and_confirms(live_page, build_agent, telemetry_db):
    page = live_page(agent=build_agent(ScriptedLLM(), clock=lambda: NOW), dev=True)
    page.get_by_role("button", name="Shift data to now").click()
    expect(page.get_by_role("status")).to_contain_text("Data shifted")
    assert latest(telemetry_db) == NOW
