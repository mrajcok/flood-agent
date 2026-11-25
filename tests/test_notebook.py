"""The Notebook module: Notebook and Cell lifecycle, Drafts, the running Cell and its errors, on real Notebook storage
and a real Table Catalog (no HTTP client, no MCP server)."""

import asyncio
import sqlite3
from contextlib import closing

import pytest

from flood_agent import ai_cell_context
from flood_agent.catalog import CellRun
from flood_agent.cell import (
    AiCell,
    AiDraft,
    AiRunSettings,
    CannotRun,
    MarkdownCell,
    MarkdownDraft,
    SqlCell,
    SqlDraft,
    SqlRunSettings,
)
from flood_agent.cell_kinds import AI, MARKDOWN, SQL
from flood_agent.events import TurnEnd
from flood_agent.model_choices import NO_MODEL
from flood_agent.notebook import NotebookBusy, UnknownCell, UnknownNotebook

SLOW = "WITH RECURSIVE n(i) AS (SELECT 1 UNION ALL SELECT i + 1 FROM n) SELECT COUNT(*) FROM n"


def ids(parts, nb) -> list[str]:
    return [c["id"] for c in parts.store.cells(nb)]


def sql_cell(parts, nb, sql: str = "SELECT dport FROM flows", max_rows: int = 1000) -> str:
    return parts.notebooks.add_cell(nb, SQL, draft=SqlDraft(sql, SqlRunSettings(max_rows, ""), ""))


async def run(parts, nb, cell) -> list:
    return [e async for e in parts.notebooks.run_cell(nb, cell)]


# --- unknown Notebook and Cell -----------------------------------------------------------------------------------


def test_an_unknown_notebook_is_a_key_error_for_every_operation(notebook_parts):
    notebooks = notebook_parts().notebooks
    for operation in (
        lambda: notebooks.rename_notebook("nope", "x"), lambda: notebooks.delete_notebook("nope"),
        lambda: notebooks.notebook_delete_preview("nope"), lambda: notebooks.add_cell("nope", AI),
        lambda: notebooks.stale_ids("nope"), lambda: notebooks.cell("nope", "c"),
    ):
        with pytest.raises(UnknownNotebook):
            operation()
    assert issubclass(UnknownNotebook, KeyError)


def test_a_cell_of_another_notebook_is_unknown_here(notebook_parts):
    parts = notebook_parts()
    nbs = parts.notebooks
    nb, other = nbs.create_notebook(), nbs.create_notebook()
    theirs = nbs.add_cell(other, AI)
    for operation in (
        lambda c: nbs.cell(nb, c), lambda c: nbs.copy_cell(nb, c), lambda c: nbs.move_cell(nb, c, "up"),
        lambda c: nbs.delete_cell(nb, c), lambda c: nbs.cell_delete_preview(nb, c),
        lambda c: nbs.delete_earlier_tables(nb, c), lambda c: nbs.markdown_copy_tables(nb, c),
        lambda c: nbs.copy_to_markdown_cell(nb, c), lambda c: nbs.stop_cell(nb, c),
        lambda c: nbs.save_draft(nb, c, AiDraft("x", AiRunSettings(NO_MODEL, None, 10))),
        lambda c: nbs.add_cell(nb, AI, "after", c),
    ):
        for cell in (theirs, "nope"):
            with pytest.raises(UnknownCell):
                operation(cell)
    assert ids(parts, other) == [theirs]  # untouched
    assert issubclass(UnknownCell, KeyError)


async def test_running_an_unknown_cell_raises_unknown_cell(notebook_parts):
    parts = notebook_parts()
    nb = parts.notebooks.create_notebook()
    with pytest.raises(UnknownCell):
        await run(parts, nb, "nope")


# --- the running Cell and NotebookBusy ---------------------------------------------------------------------------


async def start_slow(parts, nb) -> tuple[str, asyncio.Task]:
    """A SQL Cell running a query that takes far longer than the test, and the task streaming it."""
    cell = sql_cell(parts, nb, SLOW)
    task = asyncio.create_task(run(parts, nb, cell))
    for _ in range(500):
        if parts.notebooks.running_cell(nb) == cell:
            break
        await asyncio.sleep(0.01)
    assert parts.notebooks.running_cell(nb) == cell
    return cell, task


async def test_a_cell_runs_while_its_stream_is_open_and_stopping_it_records_the_run_as_stopped(notebook_parts):
    parts = notebook_parts()
    nbs = parts.notebooks
    nb = nbs.create_notebook()
    cell, task = await start_slow(parts, nb)
    assert nbs.stop_cell(nb, cell)
    events = await asyncio.wait_for(task, 10)
    assert isinstance(events[-1], TurnEnd)
    assert nbs.running_cell(nb) is None
    assert nbs.cell(nb, cell).run is not None and parts.store.cell(cell)["run"]["status"] == "stopped"
    assert nbs.stop_cell(nb, cell) is False  # no longer running


async def test_a_second_run_while_one_runs_is_refused_plainly(notebook_parts):
    parts = notebook_parts()
    nbs = parts.notebooks
    nb = nbs.create_notebook()
    slow, task = await start_slow(parts, nb)
    other = sql_cell(parts, nb, "SELECT 1 AS one")
    with pytest.raises(NotebookBusy, match="already running"):
        await run(parts, nb, other)
    assert nbs.cell(nb, other).run is None  # refused before a Run was recorded
    assert nbs.running_cell(nb) == slow
    elsewhere = nbs.create_notebook()
    await run(parts, elsewhere, sql_cell(parts, elsewhere, "SELECT 1 AS one"))  # another Notebook runs meanwhile
    nbs.stop_cell(nb, slow)
    await asyncio.wait_for(task, 10)


async def stop_when_running(nbs, nb) -> None:
    while nbs.running_cell(nb) is None:
        await asyncio.sleep(0.01)
    nbs.stop_cell(nb, nbs.running_cell(nb))


async def test_two_runs_started_together_end_with_one_run_and_the_other_refused(notebook_parts):
    parts = notebook_parts()
    nbs = parts.notebooks
    nb = nbs.create_notebook()
    a, b = sql_cell(parts, nb, SLOW), sql_cell(parts, nb, SLOW)
    results = await asyncio.wait_for(asyncio.gather(
        run(parts, nb, a), run(parts, nb, b), stop_when_running(nbs, nb), return_exceptions=True), 10)
    refused = [r for r in results[:2] if isinstance(r, NotebookBusy)]
    assert len(refused) == 1 and "already running" in str(refused[0])
    assert [nbs.cell(nb, c).run is None for c in (a, b)].count(True) == 1  # only one Run was recorded
    assert nbs.running_cell(nb) is None


async def test_a_running_cell_and_its_notebook_refuse_every_guarded_operation(notebook_parts):
    parts = notebook_parts()
    nbs = parts.notebooks
    nb = nbs.create_notebook()
    cell, task = await start_slow(parts, nb)
    for operation in (
        lambda: nbs.copy_cell(nb, cell), lambda: nbs.move_cell(nb, cell, "up"), lambda: nbs.delete_cell(nb, cell),
        lambda: nbs.cell_delete_preview(nb, cell), lambda: nbs.delete_earlier_tables(nb, cell),
        lambda: nbs.markdown_copy_tables(nb, cell), lambda: nbs.copy_to_markdown_cell(nb, cell),
        lambda: nbs.delete_notebook(nb), lambda: nbs.notebook_delete_preview(nb),
    ):
        with pytest.raises(NotebookBusy):
            operation()
    assert ids(parts, nb) == [cell]
    nbs.stop_cell(nb, cell)
    await asyncio.wait_for(task, 10)


async def test_a_draft_that_cannot_be_run_is_refused_before_a_run_and_leaves_the_notebook_idle(notebook_parts):
    parts = notebook_parts()
    nb = parts.notebooks.create_notebook()
    cell = sql_cell(parts, nb, "")
    with pytest.raises(CannotRun):
        await run(parts, nb, cell)
    assert parts.notebooks.cell(nb, cell).run is None and parts.notebooks.running_cell(nb) is None


# --- adding, copying and moving Cells ----------------------------------------------------------------------------


def test_a_cell_is_added_after_a_cell_at_the_top_or_at_the_end(notebook_parts):
    parts = notebook_parts()
    nbs = parts.notebooks
    nb = nbs.create_notebook()
    assert ids(parts, nb) == []
    a = nbs.add_cell(nb, AI, "start")  # the top of an empty Notebook
    b = nbs.add_cell(nb, SQL)
    top = nbs.add_cell(nb, MARKDOWN, "start")
    mid = nbs.add_cell(nb, AI, "after", a)
    assert ids(parts, nb) == [top, a, mid, b]
    with pytest.raises(ValueError):
        nbs.add_cell(nb, "chart")


def test_a_cell_can_be_added_holding_a_given_draft_of_its_kind(notebook_parts):
    parts = notebook_parts()
    nbs = parts.notebooks
    nb = nbs.create_notebook()
    draft = SqlDraft("SELECT 1", SqlRunSettings(5, ""), "")
    cell = nbs.add_cell(nb, SQL, draft=draft)
    assert nbs.cell(nb, cell).draft == draft
    with pytest.raises(ValueError):
        nbs.add_cell(nb, AI, draft=draft)
    assert ids(parts, nb) == [cell]


def finish(parts, cell, prompt: str, settings: dict) -> str:
    """Record a finished Run of the Cell with these settings, as a Run module would; returns its id."""
    run_id = parts.store.start_run(cell, prompt, settings, "")
    parts.store.finish_run(run_id, "ok")
    return run_id


def test_a_new_ai_cell_starts_with_the_settings_last_run(notebook_parts):
    parts = notebook_parts()
    nbs = parts.notebooks
    nb = nbs.create_notebook()
    first = nbs.add_cell(nb, AI)
    model = nbs.cell(nb, first).draft.settings.model  # the configured model, with an API key
    assert model != NO_MODEL
    finish(parts, first, "q", {"model": model, "effort": "low", "max_rows": 77})
    assert nbs.cell(nb, nbs.add_cell(nb, AI)).draft == AiDraft("", AiRunSettings(model, "low", 77))


def test_without_an_api_key_new_cells_and_old_cells_with_no_model_are_no_model(notebook_parts):
    with_key = notebook_parts()
    nb = with_key.notebooks.create_notebook()
    first = with_key.notebooks.add_cell(nb, AI)
    finish(with_key, first, "q", {"model": with_key.notebooks.cell(nb, first).draft.settings.model,
                                  "effort": "low", "max_rows": 77})
    nbs = notebook_parts(api_key=False).notebooks  # the same database, with no API key
    again = nbs.cell(nb, nbs.add_cell(nb, AI)).draft.settings
    assert again.model == NO_MODEL and again.max_rows == 77  # the last model cannot be used here; the row cap stays
    old = with_key.store.add_cell(nb, AI, {"prompt": "from before models were stored"})
    assert nbs.cell(nb, old).draft.settings.model == NO_MODEL


def test_a_new_sql_cell_starts_with_the_row_cap_last_run_and_the_sql_given(notebook_parts):
    parts = notebook_parts()
    nbs = parts.notebooks
    nb = nbs.create_notebook()
    finish(parts, nbs.add_cell(nb, SQL), "SELECT 1", {"max_rows": 42, "command": ""})
    draft = nbs.cell(nb, nbs.add_cell(nb, SQL, sql="SELECT sip FROM flows")).draft
    assert draft == SqlDraft("SELECT sip FROM flows", SqlRunSettings(42, ""), "")


def test_copying_a_cell_adds_its_draft_below_it_without_a_run(notebook_parts):
    parts = notebook_parts()
    nbs = parts.notebooks
    nb = nbs.create_notebook()
    a = sql_cell(parts, nb, "SELECT 1 AS one", max_rows=7)
    below = nbs.add_cell(nb, AI)
    finish(parts, a, "SELECT 1 AS one", {"max_rows": 7, "command": ""})
    copy = nbs.copy_cell(nb, a)
    assert ids(parts, nb) == [a, copy, below]
    assert nbs.cell(nb, copy).draft == SqlDraft("SELECT 1 AS one", SqlRunSettings(7, ""), "")
    assert nbs.cell(nb, copy).run is None


def test_moving_swaps_a_cell_with_its_neighbour_and_does_nothing_at_the_ends(notebook_parts):
    parts = notebook_parts()
    nbs = parts.notebooks
    nb = nbs.create_notebook()
    a, b, c = (nbs.add_cell(nb, AI) for _ in range(3))
    nbs.move_cell(nb, c, "up")
    assert ids(parts, nb) == [a, c, b]
    nbs.move_cell(nb, a, "up")
    nbs.move_cell(nb, b, "down")
    assert ids(parts, nb) == [a, c, b]


# --- Drafts ------------------------------------------------------------------------------------------------------


def test_a_saved_draft_is_the_cells_draft_and_must_be_of_its_kind(notebook_parts):
    parts = notebook_parts()
    nbs = parts.notebooks
    nb = nbs.create_notebook()
    cell = nbs.add_cell(nb, SQL)
    draft = SqlDraft("SELECT 2", SqlRunSettings(5, ""), "")
    nbs.save_draft(nb, cell, draft)
    assert nbs.cell(nb, cell).draft == draft
    with pytest.raises(ValueError):
        nbs.save_draft(nb, cell, AiDraft("q", AiRunSettings(NO_MODEL, None, 5)))
    assert nbs.cell(nb, cell).draft == draft


# --- renaming, deleting and their tables -------------------------------------------------------------------------


def test_renaming_trims_to_60_characters_and_refuses_a_blank_title(notebook_parts):
    parts = notebook_parts()
    nbs = parts.notebooks
    nb = nbs.create_notebook()
    nbs.rename_notebook(nb, "  " + "x" * 70 + "  ")
    assert parts.store.title_of(nb) == "x" * 60
    with pytest.raises(ValueError):
        nbs.rename_notebook(nb, "   ")
    assert parts.store.title_of(nb) == "x" * 60


def result_table(parts, owner: CellRun, name: str) -> None:
    """A Result Table `name`, with a row, made by `owner`."""
    with closing(sqlite3.connect(parts.results_db)) as con, con:
        con.execute(f"CREATE TABLE {name} (x INTEGER)")
        con.execute(f"INSERT INTO {name} VALUES (1)")
    parts.tables.register_for(owner, name)


def cell_with_tables(parts, nb, tag: str = "") -> str:
    """A SQL Cell whose earlier Run made rt_old and whose latest Run made rt_new and the Saved Table `kept` (each
    name ending in `tag`)."""
    cell = parts.notebooks.add_cell(nb, SQL)
    latest = finish(parts, cell, "SELECT 1", {"max_rows": 1, "command": ""})
    result_table(parts, CellRun(cell, "an-earlier-run"), f"rt_old{tag}")
    for name in (f"rt_new{tag}", f"rt_kept{tag}"):
        result_table(parts, CellRun(cell, latest), name)
    parts.tables.update(f"rt_kept{tag}", new_name=f"kept{tag}")
    return cell


def live(parts, cell) -> set[str]:
    return {t.name for t in parts.tables.tables_of_cell(cell)}


def test_the_delete_previews_count_result_tables_and_saved_tables(notebook_parts):
    parts = notebook_parts()
    nbs = parts.notebooks
    nb = nbs.create_notebook()
    cell_with_tables(parts, nb, "_2")
    cell = cell_with_tables(parts, nb)
    assert nbs.cell_delete_preview(nb, cell) == {"result": 2, "saved": 1}
    assert nbs.notebook_delete_preview(nb) == {"result": 4, "saved": 2}


@pytest.mark.parametrize("choice, kept", [("keep", {"rt_old", "rt_new", "kept"}), ("delete_rt", {"kept"})])
def test_deleting_a_cell_drops_its_rt_tables_only_when_asked_and_keeps_saved_tables(notebook_parts, choice, kept):
    parts = notebook_parts()
    nb = parts.notebooks.create_notebook()
    cell = cell_with_tables(parts, nb)
    parts.notebooks.delete_cell(nb, cell, choice)
    assert ids(parts, nb) == [] and live(parts, cell) == kept


@pytest.mark.parametrize("choice, kept", [("keep", {"rt_old", "rt_new", "kept"}), ("delete_rt", {"kept"})])
def test_deleting_a_notebook_drops_its_rt_tables_only_when_asked(notebook_parts, choice, kept):
    parts = notebook_parts()
    nbs = parts.notebooks
    nb = nbs.create_notebook()
    cell = cell_with_tables(parts, nb)
    nbs.delete_notebook(nb, choice)
    assert not parts.store.exists(nb) and live(parts, cell) == kept


def unreadable_results_database(parts) -> None:
    """Make dropping a table fail: the results database's path is a directory."""
    if parts.results_db.exists():
        parts.results_db.unlink()
    parts.results_db.mkdir()


def test_a_failed_table_drop_leaves_the_cell_and_the_notebook_in_place_to_retry(notebook_parts):
    parts = notebook_parts()
    nbs = parts.notebooks
    nb = nbs.create_notebook()
    cell = cell_with_tables(parts, nb)
    unreadable_results_database(parts)
    with pytest.raises(sqlite3.Error):
        nbs.delete_cell(nb, cell, "delete_rt")
    with pytest.raises(sqlite3.Error):
        nbs.delete_notebook(nb, "delete_rt")
    assert ids(parts, nb) == [cell]
    parts.results_db.rmdir()
    nbs.delete_cell(nb, cell, "delete_rt")  # the retry works
    assert ids(parts, nb) == [] and live(parts, cell) == {"kept"}


def test_deleting_earlier_tables_drops_only_earlier_runs_rt_tables(notebook_parts):
    parts = notebook_parts()
    nbs = parts.notebooks
    nb = nbs.create_notebook()
    cell = cell_with_tables(parts, nb)
    assert nbs.delete_earlier_tables(nb, cell) == ["rt_old"]
    assert live(parts, cell) == {"rt_new", "kept"}
    assert nbs.delete_earlier_tables(nb, nbs.add_cell(nb, AI)) == []  # never ran


# --- Copy to Markdown Cell ---------------------------------------------------------------------------------------


def answered_ai_cell(parts, nb) -> str:
    """An AI Cell whose latest Run answered "The answer." with table Displays of rt_a and rt_b."""
    cell = parts.notebooks.add_cell(nb, AI)
    run_id = finish(parts, cell, "q", {"model": NO_MODEL, "effort": None, "max_rows": 10})
    for name in ("rt_a", "rt_b"):
        result_table(parts, CellRun(cell, run_id), name)
    parts.store.add_step(run_id, [({"role": "assistant", "content": "The answer."},
                                   [("table", "rt_a", {}), ("table", "rt_b", {})])])
    return cell


def test_copy_to_markdown_cell_needs_an_ai_cell_that_has_run(notebook_parts):
    parts = notebook_parts()
    nbs = parts.notebooks
    nb = nbs.create_notebook()
    for cell in (nbs.add_cell(nb, AI), sql_cell(parts, nb)):
        with pytest.raises(ValueError):
            nbs.markdown_copy_tables(nb, cell)
        with pytest.raises(ValueError):
            nbs.copy_to_markdown_cell(nb, cell)
    assert len(ids(parts, nb)) == 2


@pytest.mark.parametrize("save", [
    {"rt_a": "not a name!"}, {"rt_a": "same", "rt_b": "SAME"}, {"rt_elsewhere": "mine"},
])
def test_copy_to_markdown_cell_with_a_bad_save_changes_nothing(notebook_parts, save):
    parts = notebook_parts()
    nbs = parts.notebooks
    nb = nbs.create_notebook()
    cell = answered_ai_cell(parts, nb)
    assert nbs.markdown_copy_tables(nb, cell) == ["rt_a", "rt_b"]
    with pytest.raises(ValueError):
        nbs.copy_to_markdown_cell(nb, cell, save)
    assert ids(parts, nb) == [cell] and live(parts, cell) == {"rt_a", "rt_b"}


def test_copy_to_markdown_cell_inserts_the_answer_below_and_saves_the_tables_named(notebook_parts):
    parts = notebook_parts()
    nbs = parts.notebooks
    nb = nbs.create_notebook()
    cell = answered_ai_cell(parts, nb)
    below = nbs.add_cell(nb, AI)
    copy = nbs.copy_to_markdown_cell(nb, cell, {"rt_a": "top_ports"})
    assert ids(parts, nb) == [cell, copy, below]
    draft = nbs.cell(nb, copy).draft
    assert isinstance(draft, MarkdownDraft) and draft.text == "The answer." and not draft.send_to_ai
    assert [d["table"] for d in draft.displays] == ["top_ports", "rt_b"]
    assert live(parts, cell) == {"top_ports", "rt_b"}


# --- reads for the page ------------------------------------------------------------------------------------------


def ran_with_context(parts, cell, fingerprint: str) -> None:
    """Record a finished Run of an AI Cell as sent a context with this fingerprint, as the AI Cell Run module would."""
    run_id = parts.store.start_run(cell, "q", {"model": NO_MODEL, "effort": None, "max_rows": 10}, fingerprint)
    parts.store.finish_run(run_id, "ok")


def notebook_with_a_stale_cell(parts) -> tuple[str, list[str]]:
    """A Notebook of: an AI Cell whose Run was sent what it would be sent now (nothing), an AI Cell whose Run was sent
    something else (Stale), a never-run SQL Cell and a Markdown Cell."""
    nbs = parts.notebooks
    nb = nbs.create_notebook("Ports")
    fresh, stale = nbs.add_cell(nb, AI), nbs.add_cell(nb, AI)
    ran_with_context(parts, fresh, ai_cell_context.fingerprint([]))  # nothing above it: an empty context
    ran_with_context(parts, stale, "a context from before")
    return nb, [fresh, stale, sql_cell(parts, nb), nbs.add_cell(nb, MARKDOWN)]


def test_the_notebook_page_has_its_title_its_typed_cells_in_order_and_which_are_stale(notebook_parts):
    parts = notebook_parts()
    nb, (fresh, stale, sql, note) = notebook_with_a_stale_cell(parts)
    page = parts.notebooks.notebook_page(nb)
    assert page.id == nb and page.title == "Ports"
    assert [c.cell.id for c in page.cells] == [fresh, stale, sql, note]
    assert [type(c.cell) for c in page.cells] == [AiCell, AiCell, SqlCell, MarkdownCell]
    assert [c.stale for c in page.cells] == [False, True, False, False]
    assert page.stale_ids == parts.notebooks.stale_ids(nb) == [stale]
    assert page.cells[1].run is not None and page.cells[1].run["status"] == "ok" and page.cells[2].run is None
    with pytest.raises(UnknownNotebook):
        parts.notebooks.notebook_page("nope")


def test_one_cell_read_is_typed_and_its_stale_agrees_with_the_notebook_page(notebook_parts):
    parts = notebook_parts()
    nbs = parts.notebooks
    nb, cells = notebook_with_a_stale_cell(parts)
    # a ticked note above makes the first Cell Stale too
    nbs.add_cell(nb, MARKDOWN, "start", draft=MarkdownDraft("Focus on UDP.", True, ()))
    page = nbs.notebook_page(nb)
    assert page.stale_ids == nbs.stale_ids(nb) == cells[:2]
    for shown in page.cells:
        one = nbs.page_cell(nb, shown.cell.id)
        assert (one.cell, one.run, one.stale) == (shown.cell, shown.run, shown.stale)


def test_one_cell_read_of_an_unknown_cell_or_notebook_is_refused(notebook_parts):
    parts = notebook_parts()
    nbs = parts.notebooks
    nb, other = nbs.create_notebook(), nbs.create_notebook()
    theirs = nbs.add_cell(other, AI)
    for cell in (theirs, "nope"):
        with pytest.raises(UnknownCell):
            nbs.page_cell(nb, cell)
        with pytest.raises(UnknownCell):
            nbs.run_transcript(nb, cell)
    with pytest.raises(UnknownNotebook):
        nbs.page_cell("nope", theirs)


def test_a_run_transcript_is_the_latest_runs_messages_and_displays(notebook_parts):
    parts = notebook_parts()
    nbs = parts.notebooks
    nb = nbs.create_notebook()
    assert nbs.run_transcript(nb, nbs.add_cell(nb, AI)) == []
    cell = answered_ai_cell(parts, nb)
    transcript = nbs.run_transcript(nb, cell)
    assert [i["type"] for i in transcript] == ["message", "display", "display"]
    assert transcript[0]["message"]["content"] == "The answer."
    assert [i["result_table"] for i in transcript[1:]] == ["rt_a", "rt_b"]
