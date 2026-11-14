"""Open as SQL Cell: a Trace's Generated SQL becomes a SQL Cell just below its AI Cell, ready to run."""

import re

from conftest import ScriptedLLM, tool_call
from playwright.sync_api import expect
from test_notebook_page import new_notebook

from flood_agent.llm import TextChunk

SQL = "SELECT dport FROM flows GROUP BY dport"


def test_the_cells_endpoint_takes_initial_sql_for_a_sql_cell(build_agent, web_client):
    client = web_client(build_agent(ScriptedLLM()))
    nb, first = new_notebook(client)
    added = client.post(f"/n/{nb}/cells", data={"kind": "sql", "after": first, "sql": SQL})
    assert added.status_code == 200 and 'data-kind="sql"' in added.text and SQL in added.text


def test_open_as_sql_cell_inserts_a_runnable_cell_below_the_ai_cell(live_page, build_agent):
    llm = ScriptedLLM([tool_call("run_query", sql=SQL, title="t")], [TextChunk("Done.")])
    page = live_page(agent=build_agent(llm))
    page.get_by_role("button", name="New notebook").click()
    expect(page).to_have_url(re.compile(r"/n/\w+$"))
    ai = page.locator(".cellblock.ai")
    ai.get_by_label("Prompt").fill("go")
    ai.get_by_role("button", name="Run").click()
    expect(ai.locator(".reply")).to_contain_text("Done.")
    calls = len(llm.calls)
    ai.locator("details.cell-trace > summary").click()
    ai.locator(".sql-label").get_by_role("button", name="Open as SQL Cell").click()
    cells = page.locator(".cellblock")
    expect(cells).to_have_count(2)
    sql_cell = cells.nth(1)
    expect(sql_cell).to_have_attribute("data-kind", "sql")
    expect(sql_cell.get_by_label("SQL")).to_have_value(SQL)
    sql_cell.get_by_role("button", name="Run").click()
    expect(sql_cell.locator(".output .display")).to_contain_text("8443")
    assert len(llm.calls) == calls  # no LLM

    # The footer has the same action.
    ai.locator(".cell-foot > .open-sql").click()
    expect(cells).to_have_count(3)
    expect(cells.nth(1).get_by_label("SQL")).to_have_value(SQL)
