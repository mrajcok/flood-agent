"""The Notebook page's HTTP/SSE adapter: create, open, run and stop AI Cells."""

import re
import threading
import time

from conftest import ScriptedLLM, tool_call
from test_notebooks import SQL, Slow

from flood_agent.agent import NO_MODEL
from flood_agent.llm import TextChunk

SONNET = "anthropic/claude-sonnet-5.5"


def new_notebook(client) -> tuple[str, str]:
    """Create a notebook the way the sidebar's "+" does; returns (notebook id, its first cell's id)."""
    response = client.post("/notebooks", follow_redirects=False)
    assert response.status_code == 303
    nb = response.headers["location"].removeprefix("/n/")
    page = client.get(f"/n/{nb}").text
    return nb, re.search(r'data-cell-id="(\w+)"', page).group(1)


def run_cell(client, nb, cell, prompt="show me ports", model=SONNET, effort="high", max_rows="1000"):
    fragment = client.post(f"/n/{nb}/cells/{cell}/run", data={
        "prompt": prompt, "model": model, "effort": effort, "max_rows": max_rows})
    assert fragment.status_code == 200
    stream = client.get(f"/n/{nb}/cells/{cell}/stream").text
    return fragment.text, stream, client.get(f"/n/{nb}/cells/{cell}").text


def test_the_sidebar_plus_creates_notebook_n_with_one_empty_ai_cell(build_agent, web_client):
    client = web_client(build_agent(ScriptedLLM()))
    nb, cell = new_notebook(client)

    page = client.get(f"/n/{nb}")
    assert "Notebook 1" in page.text
    assert page.text.count('data-kind="ai"') == 1
    assert 'class="current"' in page.text  # the sidebar marks it
    assert f'href="/n/{nb}"' in page.text
    assert "[ ]" in page.text  # never run
    assert "Notebook 1" in client.get("/").text  # listed beside Conversations
    second, _ = new_notebook(client)
    assert "Notebook 2" in client.get(f"/n/{second}").text


def test_an_unknown_notebook_or_cell_is_404(build_agent, web_client):
    client = web_client(build_agent(ScriptedLLM()))
    nb, cell = new_notebook(client)
    assert client.get("/n/nope").status_code == 404
    assert client.get(f"/n/{nb}/cells/nope").status_code == 404
    assert client.get(f"/n/nope/cells/{cell}").status_code == 404


def test_running_a_cell_streams_the_answer_and_its_footer_shows_the_run(build_agent, web_client):
    llm = ScriptedLLM([tool_call("run_query", sql=SQL, title="t")], [TextChunk("Port 8443 is busiest.")])
    client = web_client(build_agent(llm))
    nb, cell = new_notebook(client)

    started, stream, done = run_cell(client, nb, cell)

    assert f"/n/{nb}/cells/{cell}/stream" in started and "[*]" in started
    assert "event: live-step" in stream and "LLM call 1" in stream
    assert "event: text" in stream and "Port 8443 is busiest." in stream
    assert stream.rstrip().endswith("event: end\ndata: done")
    assert "[1]" in done and "Port 8443 is busiest." in done
    assert "2 LLM calls" in done and "$0.002" in done
    assert "Claude Sonnet 5.5 · high · ≤ 1,000 rows" in done
    assert "ran" in done and "stopped" not in done


def test_the_context_meter_uses_the_window_of_the_model_the_cell_ran_with(build_agent, web_client):
    client = web_client(build_agent(ScriptedLLM([TextChunk("a")], [TextChunk("b")], [TextChunk("c")])))
    nb, cell = new_notebook(client)  # the ScriptedLLM reports 100 prompt tokens
    assert "100 tokens of 1M" in run_cell(client, nb, cell, model=SONNET)[2]
    assert "100 tokens of 1.02M" in run_cell(client, nb, cell, model="qwen/qwen3.8-27b", effort="low")[2]
    assert "no context used" in run_cell(client, nb, cell, model=NO_MODEL, effort="")[2]


def test_stopping_a_running_cell_keeps_its_text_and_shows_a_stopped_badge(build_agent, web_client):
    client = web_client(build_agent(Slow()))
    nb, cell = new_notebook(client)
    client.post(f"/n/{nb}/cells/{cell}/run", data={
        "prompt": "p", "model": SONNET, "effort": "", "max_rows": "1000"})
    stream: list[str] = []
    reader = threading.Thread(target=lambda: stream.append(client.get(f"/n/{nb}/cells/{cell}/stream").text))
    reader.start()
    for _ in range(100):
        if "Partial answer" in client.get(f"/n/{nb}/cells/{cell}").text:
            break
        time.sleep(0.05)
    assert client.post(f"/n/{nb}/cells/{cell}/stop").status_code == 204
    reader.join(5)

    done = client.get(f"/n/{nb}/cells/{cell}").text
    assert "Partial answer" in done
    assert "stopped by the user" in done and 'class="badge stopped"' in done


def test_a_second_run_in_the_same_notebook_is_refused_while_one_runs(build_agent, web_client):
    client = web_client(build_agent(Slow()))
    nb, cell = new_notebook(client)
    other = client.post(f"/n/{nb}/cells", data={"kind": "ai", "after": cell}).text
    other_id = re.search(r'data-cell-id="(\w+)"', other).group(1)
    reader = threading.Thread(target=lambda: client.get(f"/n/{nb}/cells/{cell}/stream"))
    client.post(f"/n/{nb}/cells/{cell}/run", data={"prompt": "p", "model": SONNET, "effort": "", "max_rows": "5"})
    reader.start()
    for _ in range(100):
        if "[*]" in client.get(f"/n/{nb}/cells/{cell}").text:
            break
        time.sleep(0.05)
    refused = client.post(f"/n/{nb}/cells/{other_id}/run", data={
        "prompt": "q", "model": SONNET, "effort": "", "max_rows": "5"})
    assert refused.status_code == 409
    client.post(f"/n/{nb}/cells/{cell}/stop")
    reader.join(5)


def test_a_run_needs_a_prompt_and_a_known_model_effort_and_row_cap(build_agent, web_client):
    client = web_client(build_agent(ScriptedLLM()))
    nb, cell = new_notebook(client)
    ok = {"prompt": "p", "model": SONNET, "effort": "high", "max_rows": "10"}
    for bad in ({"prompt": "  "}, {"model": "nope"}, {"effort": "bogus"}, {"max_rows": "0"}, {"max_rows": "x"}):
        assert client.post(f"/n/{nb}/cells/{cell}/run", data={**ok, **bad}).status_code == 422, bad


def test_a_new_ai_cell_starts_with_the_last_settings_run(build_agent, web_client):
    client = web_client(build_agent(ScriptedLLM([TextChunk("a")])))
    nb, cell = new_notebook(client)
    run_cell(client, nb, cell, model=SONNET, effort="low", max_rows="50")

    added = client.post(f"/n/{nb}/cells", data={"kind": "ai", "after": cell})
    assert added.status_code == 200
    assert "Claude Sonnet 5.5 · low · ≤ 50 rows" in added.text
    assert added.text.count('data-kind="ai"') == 1
    page = client.get(f"/n/{nb}").text
    assert page.index(cell) < page.index(re.findall(r'data-cell-id="(\w+)"', added.text)[0])  # after the first cell
    assert client.post(f"/n/{nb}/cells", data={"kind": "nonsense"}).status_code == 422


def test_a_cells_draft_is_saved_and_shown_when_the_page_is_opened_again(build_agent, web_client):
    client = web_client(build_agent(ScriptedLLM()))
    nb, cell = new_notebook(client)
    saved = client.post(f"/n/{nb}/cells/{cell}/draft", data={
        "prompt": "half-written", "model": NO_MODEL, "effort": "", "max_rows": "7"})
    assert saved.status_code == 204
    page = client.get(f"/n/{nb}").text
    assert "half-written" in page and "No model · no effort · ≤ 7 rows" in page
