"""Fake Mode in AI Cells: Canned Prompts run per cell; a follow-up needs its parent in an AI Cell above."""

import json
import re

import pytest
from conftest import all_telemetry_values

from flood_agent.agent import NO_MODEL, DisplayEvent, TextDelta, TraceReady, TurnError
from flood_agent.fake_llm import FakeClient, load_canned_prompts, parent_of

CANNED = load_canned_prompts()
FAKE_CAP = 20_000


async def run(agent, nb, cell):
    return [ev async for ev in agent.run_cell(nb, cell)]


def text(events) -> str:
    return "".join(e.text for e in events if isinstance(e, TextDelta))


async def fake_agent(make_agent):
    agent = await make_agent(FakeClient(CANNED, chunk_delay_s=0))
    return agent, agent.create_notebook()


def follow_up():
    f = next(c for c in CANNED if c.after)
    return f, parent_of(f, CANNED)


async def test_a_follow_up_with_no_parent_above_says_to_run_it_first_in_an_ai_cell_above(make_agent):
    agent, nb = await fake_agent(make_agent)
    f, parent = follow_up()
    cell = agent.add_ai_cell(nb, prompt=f.prompt, model=NO_MODEL)

    events = await run(agent, nb, cell)

    assert f"Fake Mode: run *{parent.prompt}* first in an AI Cell above" in text(events)
    assert not [e for e in events if isinstance(e, DisplayEvent)]


async def test_a_follow_up_whose_parent_is_only_below_it_says_to_run_it_first(make_agent):
    agent, nb = await fake_agent(make_agent)
    f, parent = follow_up()
    below_first = agent.add_ai_cell(nb, prompt=f.prompt, model=NO_MODEL)
    agent.add_ai_cell(nb, prompt=parent.prompt, model=NO_MODEL)
    await run(agent, nb, agent.notebooks.cells(nb)[1]["id"])

    events = await run(agent, nb, below_first)

    assert "first in an AI Cell above" in text(events)


async def test_a_follow_up_below_its_parents_cell_resolves_the_parents_tables(make_agent):
    agent, nb = await fake_agent(make_agent)
    f, parent = follow_up()
    p = agent.add_ai_cell(nb, prompt=parent.prompt, model=NO_MODEL, max_rows=FAKE_CAP)
    c = agent.add_ai_cell(nb, prompt=f.prompt, model=NO_MODEL, max_rows=FAKE_CAP)
    await run(agent, nb, p)

    events = await run(agent, nb, c)

    assert [e.kind for e in events if isinstance(e, DisplayEvent)] == list(f.expect.displays)
    assert "first in an AI Cell above" not in text(events)


@pytest.mark.parametrize("canned", CANNED, ids=lambda c: c.id)
async def test_every_canned_prompt_run_in_a_notebook_meets_its_declared_expectations(canned, make_agent, telemetry_db):
    agent, nb = await fake_agent(make_agent)
    parent = parent_of(canned, CANNED)
    if parent:
        await run(agent, nb, agent.add_ai_cell(nb, prompt=parent.prompt, model=NO_MODEL, max_rows=FAKE_CAP))
    cell = agent.add_ai_cell(nb, prompt=canned.prompt, model=NO_MODEL, max_rows=FAKE_CAP)

    events = await run(agent, nb, cell)

    assert [e.kind for e in events if isinstance(e, DisplayEvent)] == list(canned.expect.displays)
    trace = next(e.trace for e in events if isinstance(e, TraceReady))
    assert trace["model"] == NO_MODEL
    failed = [e for e in trace["entries"]
              if e["kind"] == "tool_call" and not json.loads(e["query_metadata"]).get("success")]
    assert len(failed) == canned.expect.failed_tool_calls
    assert canned.expect.notice in text(events)
    assert any(isinstance(e, TurnError) for e in events) == canned.expect.turn_error
    values = {v for v in all_telemetry_values(telemetry_db) if len(v) >= 4}
    stored = re.sub(r"rt_\w+", "", json.dumps(agent.notebooks.transcript(agent.notebooks.cell(cell)["run"]["id"])))
    assert not any(v in stored for v in values)
