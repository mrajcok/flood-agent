"""The AI Cell context module, tested by passing a Notebook's cells: no store, no LLM.

The goldens in ai_cell_context_golden.json were captured from the Agent's code before the module existed:
messages and fingerprints must stay byte-identical, so a mismatch means the change is wrong, not the golden."""

import copy
import json
from pathlib import Path

import pytest

from flood_agent import ai_cell_context as ctx

GOLDEN = json.loads((Path(__file__).parent / "ai_cell_context_golden.json").read_text())


def _run(id, status="ok", messages=(), fp="x", trace=None):
    return {"id": id, "status": status, "messages": list(messages), "context_fingerprint": fp, "trace": trace}


def _cell(id, kind, draft=None, run=None):
    return {"id": id, "kind": kind, "draft": draft or {}, "run": run}


def _call(i):
    return {"id": i, "type": "function", "function": {"name": "run_query", "arguments": "{\"sql\": \"select 1\"}"}}


def _cells():
    return [
        _cell("md1", "markdown", {"text": " Notes here ", "send_to_ai": True}),
        _cell("md2", "markdown", {"text": "unticked", "send_to_ai": False}),
        _cell("md3", "markdown", {"text": "with disp", "send_to_ai": True, "displays": [
            {"kind": "table", "table": "r_1", "rows": [[1]]}, {"kind": "chart", "table": "r_2", "spec": {"a": 1}}]}),
        _cell("md4", "markdown", {"text": "", "send_to_ai": True, "displays": [{"kind": "table", "table": "r_3"}]}),
        _cell("md5", "markdown", {"text": "  ", "send_to_ai": True}),
        _cell("sql1", "sql", {"sql": "select 1"}, _run("rs1", fp="", messages=[
            {"role": "user", "content": "[SQL Cell, run by the user] select 1\n1 row"}])),
        _cell("sql2", "sql", {}, _run("rs2", fp="", messages=[
            {"role": "user", "content": "[SHOW Cell, run by the user] SHOW TABLES\nx"}])),
        _cell("ai1", "ai", {"prompt": "q1"}, _run("ra1", messages=[
            {"role": "user", "content": "q1"},
            {"role": "assistant", "content": None, "tool_calls": [_call("c1")]},
            {"role": "tool", "tool_call_id": "c1", "content": "{\"ok\": 1}"},
            {"role": "assistant", "content": "answer1"}])),
        _cell("ai2", "ai", {"prompt": "q2"}, _run("ra2", "stopped", [
            {"role": "user", "content": "q2"}, {"role": "assistant", "content": "partial\n\n*Stopped by user.*"}])),
        _cell("ai2b", "ai", {"prompt": "q2b"}, _run("ra2b", "stopped", [{"role": "user", "content": "q2b"}])),
        _cell("ai3", "ai", {"prompt": "q3"}, _run("ra3", "failed", [
            {"role": "user", "content": "q3"}, {"role": "assistant", "content": "boom"}],
            trace={"entries": [{"kind": "error", "message": "boom"}]})),
        _cell("ai3b", "ai", {"prompt": "q3b"}, _run("ra3b", "failed", [
            {"role": "user", "content": "q3b"}, {"role": "assistant", "content": "txt"}])),
        _cell("ai4", "ai", {"prompt": "q4"}, _run("ra4", messages=[  # corrupt: a call with a missing result, an orphan
            {"role": "user", "content": "q4"},
            {"role": "assistant", "content": None, "tool_calls": [_call("d1"), _call("d2")]},
            {"role": "tool", "tool_call_id": "d1", "content": "{}"},
            {"role": "tool", "tool_call_id": "zz", "content": "orphan"},
            {"role": "assistant", "content": "a4"}])),
        _cell("ai5", "ai", {"prompt": "q5"}, _run("ra5", "running", [{"role": "user", "content": "q5"}])),
        _cell("last", "ai", {"prompt": "q"}),
    ]


@pytest.mark.parametrize("cell_id", list(GOLDEN))
def test_what_a_cell_is_sent_is_byte_identical_to_before(cell_id):
    context = ctx.context_above(_cells(), cell_id)
    want = GOLDEN[cell_id]
    assert context.messages == want["messages"]
    assert [list(pair) for pair in context.above] == want["above"]
    assert context.fingerprint == want["fp"]


def test_goldens_cover_the_cases_the_ticket_names():
    sent = [m["content"] for m in GOLDEN["last"]["messages"] if m["role"] == "user"]
    assert "[Markdown note] Notes here" in sent
    assert "[Markdown note] with disp\n[Display: table r_1]\n[Display: chart r_2]" in sent
    assert "[Markdown note] unticked" not in sent
    assert any(s.startswith(ctx.SQL_CELL_LABEL) for s in sent) and any(s.startswith(ctx.SHOW_CELL_LABEL) for s in sent)
    final = [m["content"] for m in GOLDEN["last"]["messages"] if m["role"] == "assistant"]
    assert "partial\n\n(stopped by the user)" in final and "failed: boom" in final


def _stored(cells):
    """Give every finished AI Cell the fingerprint it was sent, as a real Run stores it."""
    for cell in cells:
        if cell["kind"] == "ai" and cell["run"] and cell["run"]["status"] != "running":
            cell["run"]["context_fingerprint"] = ctx.context_above(cells, cell["id"]).fingerprint
    return cells


def _notebook():
    return _stored([
        _cell("md", "markdown", {"text": "note", "send_to_ai": False}),
        _cell("a", "ai", {"prompt": "qa"}, _run("ra", messages=[
            {"role": "user", "content": "qa"}, {"role": "assistant", "content": "aa"}])),
        _cell("b", "ai", {"prompt": "qb"}, _run("rb", messages=[
            {"role": "user", "content": "qb"}, {"role": "assistant", "content": "ab"}])),
    ])


def test_an_untouched_notebook_has_nothing_stale():
    assert ctx.stale_cells(_notebook()) == set()


def test_a_cell_above_edited_and_run_makes_the_cells_below_stale():
    cells = _notebook()
    cells[1]["run"]["messages"][1]["content"] = "new answer"
    assert ctx.stale_cells(cells) == {"b"}


def test_moving_a_cell_above_makes_cells_stale():
    cells = _notebook()
    cells[1], cells[2] = cells[2], cells[1]
    assert ctx.stale_cells(cells) == {"a", "b"}


def test_deleting_a_cell_above_makes_cells_below_stale():
    cells = _notebook()
    del cells[1]
    assert ctx.stale_cells(cells) == {"b"}


def test_toggling_send_to_ai_makes_cells_below_stale_and_back_again_clears_it():
    cells = _notebook()
    cells[0]["draft"]["send_to_ai"] = True
    assert ctx.stale_cells(cells) == {"a", "b"}
    cells[0]["draft"]["send_to_ai"] = False
    assert ctx.stale_cells(cells) == set()


def test_a_draft_alone_changes_nothing():
    cells = _notebook()
    cells[1]["draft"]["prompt"] = "something else"
    assert ctx.stale_cells(cells) == set()


def test_sql_running_never_ran_and_unfingerprinted_cells_are_never_stale():
    cells = _notebook()
    cells[1]["run"]["messages"][1]["content"] = "new answer"  # b would be stale, but:
    cells[2]["run"]["status"] = "running"
    cells.append(_cell("sql", "sql", {}, _run("rs", fp="", messages=[{"role": "user", "content": "x"}])))
    cells.append(_cell("never", "ai", {"prompt": "q"}))
    cells.append(_cell("old", "ai", {"prompt": "q"}, _run("ro", fp="")))
    assert ctx.stale_cells(cells) == set()


def test_the_cells_passed_in_are_not_modified():
    cells = _cells()
    before = copy.deepcopy(cells)
    ctx.context_above(cells, "last")
    ctx.stale_cells(cells)
    assert cells == before


def test_listed_result_tables_are_those_of_cells_above_then_the_cells_own_run_by_creation_time_then_name():
    from types import SimpleNamespace as T
    made = {
        ("a", "ra"): [T(name="rt_b", created_at_ms=2), T(name="rt_a", created_at_ms=2)],
        ("b", "rb"): [T(name="rt_old", created_at_ms=1)],
        ("me", "rme"): [T(name="rt_own", created_at_ms=3)],
    }
    asked = []

    def tables_of_run(cell_id, run_id):
        asked.append((cell_id, run_id))
        return made.get((cell_id, run_id), [])

    listed = ctx.listed_result_tables([("a", "ra"), ("b", "rb")], "me", "rme", tables_of_run)
    assert [t.name for t in listed] == ["rt_old", "rt_a", "rt_b", "rt_own"]
    assert asked == [("a", "ra"), ("b", "rb"), ("me", "rme")]
