"""The Notebook page's HTTP/SSE adapter: create, open, run and stop AI Cells."""

import re
import threading
import time

from conftest import ScriptedLLM, ask, main_pane, table_script, tool_call
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


def test_a_notebook_survives_a_restart_with_its_answer_display_and_sidebar_entry(build_agent, web_client):
    client = web_client(build_agent(ScriptedLLM(*table_script())))
    nb, _, _ = ask(client, "top ports by bytes")

    restarted = web_client(build_agent(ScriptedLLM()))  # new app, same files
    home = restarted.get("/").text
    assert f'href="/n/{nb}"' in home and "Notebook 1" in home

    page = restarted.get(f"/n/{nb}").text
    assert "top ports by bytes" in main_pane(page) and "Here you go." in page
    rows_url = page.split('data-rows-url="')[1].split('"')[0]
    assert restarted.get(rows_url).json()["rows"] == [[8443, 1_111_111], [9053, 555_321]]  # the Display re-renders


def test_a_cell_run_after_a_restart_is_sent_the_cells_above(build_agent, web_client):
    client = web_client(build_agent(ScriptedLLM(*table_script())))
    nb, _, _ = ask(client, "first question")

    second = ScriptedLLM([TextChunk("ok")])
    restarted = web_client(build_agent(second))
    ask(restarted, "second question", nb)
    sent = second.all_sent_text()
    assert "first question" in sent and "second question" in sent


def test_notebooks_are_separate_in_what_a_cell_is_sent_and_in_the_tables_shown(build_agent, web_client):
    llm = ScriptedLLM(*table_script("first reply"), *table_script("second reply"))
    client = web_client(build_agent(llm))
    one, _, body1 = ask(client, "alpha question")
    two, _, body2 = ask(client, "beta question")
    assert one != two
    assert not any("alpha question" in str(m.get("content")) for m in llm.calls[-1])

    t1 = body1.split('data-rows-url="')[1].split('"')[0]
    t2 = body2.split('data-rows-url="')[1].split('"')[0]
    page1, page2 = client.get(f"/n/{one}").text, client.get(f"/n/{two}").text
    assert t1 != t2 and t1 in page1 and t2 not in page1 and t2 in page2 and t1 not in page2
    assert "alpha question" not in main_pane(page2)
    assert "Notebook 1" in client.get("/").text and "Notebook 2" in client.get("/").text


def test_the_stream_sends_each_llm_call_as_a_live_step_with_its_entries(build_agent, web_client):
    client = web_client(build_agent(ScriptedLLM([TextChunk("hi")])))
    nb, cell = new_notebook(client)
    fragment = client.post(f"/n/{nb}/cells/{cell}/run", data={
        "prompt": "hello", "model": SONNET, "effort": "", "max_rows": "1000"}).text
    assert "thinking" not in fragment and 'sse-swap="live-step"' in fragment  # no spinner: Send turns into Stop
    body = client.get(f"/n/{nb}/cells/{cell}/stream").text
    assert "event: live-step" in body and 'id="live-body-' in body
    assert "event: entry" in body and 'hx-swap-oob="beforeend:#live-body-' in body
    assert 'hx-swap-oob="innerHTML:#live-meta-' in body
    assert body.index("event: live-step") < body.index("event: entry")


def test_stopping_a_cell_that_is_not_running_is_harmless(build_agent, web_client):
    client = web_client(build_agent(ScriptedLLM()))
    nb, cell = new_notebook(client)
    assert client.post(f"/n/{nb}/cells/{cell}/stop").status_code == 204
    assert client.post(f"/n/{nb}/cells/nope/stop").status_code == 404


def test_deleting_a_later_cell_leaves_an_earlier_cells_table_readable_and_foreign_cells_404(build_agent, web_client):
    client = web_client(build_agent(ScriptedLLM(*table_script("one"), *table_script("two"), [TextChunk("other")])))
    nb, first, _ = ask(client, "first question")
    _, second, _ = ask(client, "second question", nb)
    other, other_cell, _ = ask(client, "elsewhere")
    first_table = re.search(r"/api/results/([a-z0-9_]+)/rows", client.get(f"/n/{nb}/cells/{first}").text).group(1)

    assert client.post(f"/n/{nb}/cells/{second}/delete", data={"tables": "delete_rt"}).status_code == 204
    assert client.get(f"/api/results/{first_table}/rows").status_code == 200  # made by the first cell
    assert client.post(f"/n/{nb}/cells/{other_cell}/delete").status_code == 404  # another notebook's cell
    assert client.post(f"/n/{other}/cells/{second}/delete").status_code == 404  # a cell that is gone
    assert "first question" in client.get(f"/n/{nb}").text and "elsewhere" in client.get(f"/n/{other}").text


def test_no_notebook_text_says_session(build_agent, web_client):
    client = web_client(build_agent(ScriptedLLM([TextChunk("hi")])))
    nb, _, _ = ask(client, "hi")
    page = client.get(f"/n/{nb}").text
    assert 'aria-label="New notebook"' in page
    assert "session" not in re.sub(r"<script.*?</script>", "", page, flags=re.S).lower()
