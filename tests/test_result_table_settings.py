"""A Result Table's user-editable name and retention, at the HTTP seam."""

import re

from conftest import ScriptedLLM, last_tool_result, tool_call

from flood_agent.llm import TextChunk
from flood_agent.web import TEMPLATES

BY_PORT = "SELECT dport, SUM(bytes) AS total_bytes FROM flows GROUP BY dport"
DAY = 24 * 3_600_000


class Clock:
    def __init__(self, ms=1_000_000):
        self.ms = ms

    def __call__(self):
        return self.ms


def shown_table(build_agent, web_client):
    clock = Clock()
    llm = ScriptedLLM(
        [tool_call("run_query", sql=BY_PORT, title="Bytes per port")],
        lambda msgs: [tool_call("show_table", result_table=last_tool_result(msgs)["result_table"])],
        [TextChunk("here")],
    )
    client = web_client(build_agent(llm, clock=clock, result_ttl_ms=DAY))
    posted = client.post("/chat", data={"message": "ports", "conversation_id": "", "max_rows": "1000"})
    body = client.get(posted.text.split('sse-connect="')[1].split('"')[0]).text
    rows_url = body.split('data-rows-url="')[1].split('"')[0]
    return client, clock, re.search(r"/api/results/([^/]+)/rows", rows_url).group(1)


def test_rows_report_the_name_and_default_retention(build_agent, web_client):
    client, _, name = shown_table(build_agent, web_client)
    data = client.get(f"/api/results/{name}/rows").json()
    assert data["title"] == "Bytes per port" and data["retention_days"] == 1


def test_display_offers_name_and_retention_editor(build_agent, web_client):
    _, _, name = shown_table(build_agent, web_client)
    html = TEMPLATES.get_template("display_table.html").render(
        rows_url="/r", psv_url="/p", result_table=name, columns=[], settings_url=f"/api/results/{name}/settings")
    for choice in ("1 day", "7 days", "30 days", "365 days", "Custom"):
        assert choice in html
    assert "days" in html and 'type="number"' in html


def test_rename_persists(build_agent, web_client):
    client, _, name = shown_table(build_agent, web_client)
    resp = client.post(f"/api/results/{name}/settings", data={"title": "  Top ports  "})
    assert resp.status_code == 200 and resp.json()["title"] == "Top ports"
    assert client.get(f"/api/results/{name}/rows").json()["title"] == "Top ports"


def test_blank_name_and_bad_retention_are_rejected(build_agent, web_client):
    client, _, name = shown_table(build_agent, web_client)
    url = f"/api/results/{name}/settings"
    assert client.post(url, data={"title": "   "}).status_code == 422
    for bad in ("0", "-3", "abc", "nan", "inf", "100000"):
        assert client.post(url, data={"retention_days": bad}).status_code == 422, bad
    assert client.post("/api/results/r_nope/settings", data={"title": "x"}).status_code == 404
    assert client.get(f"/api/results/{name}/rows").json()["retention_days"] == 1


def test_longer_retention_keeps_the_table_alive(build_agent, web_client):
    client, clock, name = shown_table(build_agent, web_client)
    assert client.post(f"/api/results/{name}/settings", data={"retention_days": "7"}).json()["retention_days"] == 7
    clock.ms += 7 * DAY - 1
    assert client.get(f"/api/results/{name}/rows").status_code == 200
    clock.ms += 2
    assert client.get(f"/api/results/{name}/rows").status_code == 410


def test_shorter_retention_expires_sooner(build_agent, web_client):
    client, clock, name = shown_table(build_agent, web_client)
    client.post(f"/api/results/{name}/settings", data={"retention_days": "0.5"})
    clock.ms += DAY // 2 + 1
    assert client.get(f"/api/results/{name}/rows").status_code == 410


def test_settings_on_expired_table_is_410(build_agent, web_client):
    client, clock, name = shown_table(build_agent, web_client)
    clock.ms += DAY + 1
    assert client.post(f"/api/results/{name}/settings", data={"title": "x"}).status_code == 410
