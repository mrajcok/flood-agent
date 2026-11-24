"""The Cell module, tested by passing the rows Notebook storage returns: no HTTP client, no MCP server."""

import pytest

from flood_agent.cell import (
    AiCell,
    AiDraft,
    AiRunSettings,
    LatestAiRun,
    LatestSqlRun,
    MarkdownCell,
    MarkdownDraft,
    ModelEfforts,
    SqlCell,
    SqlDraft,
    SqlRunSettings,
    save_draft,
    starting_ai_draft,
    starting_sql_draft,
    typed_cell,
)
from flood_agent.notebooks import DEFAULT_MAX_ROWS, NotebookStore

DEFAULT_MODEL = "default/model"


def _row(kind, draft, run=None, id="c1", notebook_id="nb1"):
    return {"id": id, "notebook_id": notebook_id, "kind": kind, "draft": draft, "run": run}


def test_an_ai_cell_is_built_with_its_draft():
    cell = typed_cell(_row("ai", {"prompt": "top ports", "model": "m/x", "effort": "high", "max_rows": 50}),
                      DEFAULT_MODEL)

    assert cell == AiCell("c1", "nb1", AiDraft("top ports", AiRunSettings("m/x", "high", 50)), None)
    assert cell.prompt == "top ports"
    assert cell.run_settings == AiRunSettings("m/x", "high", 50)


def test_an_old_ai_cell_without_a_model_or_row_cap_runs_with_the_defaults():
    cell = typed_cell(_row("ai", {"prompt": "q"}), DEFAULT_MODEL)

    assert cell.run_settings == AiRunSettings(DEFAULT_MODEL, None, DEFAULT_MAX_ROWS)


def test_an_ai_cell_holds_the_prompt_and_settings_of_its_latest_run():
    run = {"id": "r1", "status": "ok", "prompt": "ran this", "settings": {"model": "m/y", "effort": None, "max_rows": 10}}
    cell = typed_cell(_row("ai", {"prompt": "q", "model": "m/x", "effort": "low", "max_rows": 5}, run), DEFAULT_MODEL)

    assert cell.run == LatestAiRun("r1", "ran this", AiRunSettings("m/y", None, 10))


def test_a_sql_cell_runs_its_query_with_its_row_cap():
    cell = typed_cell(_row("sql", {"sql": "SELECT 1", "max_rows": 20, "command": "", "table": ""}), DEFAULT_MODEL)

    assert cell == SqlCell("c1", "nb1", SqlDraft("SELECT 1", SqlRunSettings(20, ""), ""), None)
    assert cell.statement == "SELECT 1"
    assert cell.run_settings == SqlRunSettings(20, "")


def test_an_old_sql_cell_without_a_row_cap_or_command_runs_its_query_with_the_default_row_cap():
    cell = typed_cell(_row("sql", {"sql": "SELECT 1"}), DEFAULT_MODEL)

    assert cell.statement == "SELECT 1"
    assert cell.run_settings == SqlRunSettings(DEFAULT_MAX_ROWS, "")


def test_a_sql_cell_with_a_show_command_runs_the_command_not_its_sql():
    cell = typed_cell(_row("sql", {"sql": "SELECT 1", "max_rows": 20, "command": "SHOW TABLES", "table": ""}),
                      DEFAULT_MODEL)

    assert cell.statement == "SHOW TABLES"
    assert cell.run_settings == SqlRunSettings(20, "SHOW TABLES")


def test_a_describe_table_cell_runs_the_command_with_its_table():
    cell = typed_cell(_row("sql", {"sql": "", "max_rows": 20, "command": "DESCRIBE TABLE", "table": " flows "}),
                      DEFAULT_MODEL)

    assert cell.statement == "DESCRIBE TABLE flows"
    assert cell.run_settings == SqlRunSettings(20, "DESCRIBE TABLE")


def test_a_sql_cell_holds_the_statement_and_settings_of_its_latest_run():
    run = {"id": "r1", "status": "failed", "prompt": "SHOW TABLES", "settings": {"max_rows": 7, "command": "SHOW TABLES"}}
    cell = typed_cell(_row("sql", {"sql": "SELECT 1"}, run), DEFAULT_MODEL)

    assert cell.run == LatestSqlRun("r1", "SHOW TABLES", SqlRunSettings(7, "SHOW TABLES"))


def test_a_markdown_cell_is_built_with_its_text_send_to_ai_and_displays():
    displays = [{"kind": "chart", "table": "rt_1", "spec": {"mark": "bar"}}]
    cell = typed_cell(_row("markdown", {"text": "# Notes", "send_to_ai": True, "displays": displays}), DEFAULT_MODEL)

    assert cell == MarkdownCell("c1", "nb1", MarkdownDraft("# Notes", True, ({"kind": "chart", "table": "rt_1",
                                                                              "spec": {"mark": "bar"}},)))


def test_a_markdown_cell_without_displays_or_send_to_ai_has_none_and_is_not_sent():
    cell = typed_cell(_row("markdown", {"text": "x"}), DEFAULT_MODEL)

    assert cell.draft == MarkdownDraft("x", False, ())


# --- saving a Draft, through a real Notebook storage --------------------------------------------------------------


def _stored_cell(tmp_path, kind, draft):
    store = NotebookStore(tmp_path / "app.db")
    return store, store.add_cell(store.create_notebook(), kind, draft)


def _saved(store, cell_id):
    return typed_cell(store.cell(cell_id), DEFAULT_MODEL).draft


def test_an_ai_draft_is_saved_whole(tmp_path):
    store, cell_id = _stored_cell(tmp_path, "ai", {"prompt": "old", "model": "m/x", "effort": "low", "max_rows": 5})

    save_draft(store, cell_id, AiDraft("top ports", AiRunSettings("m/y", None, 50)))

    assert _saved(store, cell_id) == AiDraft("top ports", AiRunSettings("m/y", None, 50))


def test_a_sql_draft_is_saved_whole(tmp_path):
    store, cell_id = _stored_cell(tmp_path, "sql", {"sql": "SELECT 1", "max_rows": 5, "command": "", "table": ""})

    save_draft(store, cell_id, SqlDraft("", SqlRunSettings(20, "DESCRIBE TABLE"), "flows"))

    assert _saved(store, cell_id) == SqlDraft("", SqlRunSettings(20, "DESCRIBE TABLE"), "flows")


def test_a_markdown_draft_is_saved_whole_with_its_displays(tmp_path):
    store, cell_id = _stored_cell(tmp_path, "markdown", {"text": "old", "send_to_ai": True})
    displays = ({"kind": "chart", "table": "rt_1", "spec": {"mark": "bar"}},)

    save_draft(store, cell_id, MarkdownDraft("# Notes", False, displays))

    assert _saved(store, cell_id) == MarkdownDraft("# Notes", False, displays)


def test_a_draft_that_cannot_be_run_is_still_saved(tmp_path):
    store, cell_id = _stored_cell(tmp_path, "ai", {"prompt": "old", "model": "m/x", "effort": None, "max_rows": 5})

    save_draft(store, cell_id, AiDraft("", AiRunSettings("m/x", None, 5)))

    assert _saved(store, cell_id).prompt == ""


def test_saving_a_draft_leaves_the_latest_run_in_place(tmp_path):
    store, cell_id = _stored_cell(tmp_path, "ai", {"prompt": "ran", "model": "m/x", "effort": None, "max_rows": 5})
    run_id = store.start_run(cell_id, "ran", {"model": "m/x", "effort": None, "max_rows": 5}, "fp")

    save_draft(store, cell_id, AiDraft("edited", AiRunSettings("m/x", None, 5)))

    assert typed_cell(store.cell(cell_id), DEFAULT_MODEL).run == LatestAiRun(run_id, "ran", AiRunSettings("m/x", None, 5))


@pytest.mark.parametrize("kind, draft", [
    ("ai", SqlDraft("SELECT 1", SqlRunSettings(5, ""), "")),
    ("sql", AiDraft("q", AiRunSettings("m/x", None, 5))),
    ("markdown", AiDraft("q", AiRunSettings("m/x", None, 5))),
    ("ai", MarkdownDraft("text", False, ())),
])
def test_a_draft_of_the_wrong_kind_is_refused_and_the_stored_draft_kept(tmp_path, kind, draft):
    stored = {"ai": {"prompt": "kept"}, "sql": {"sql": "kept"}, "markdown": {"text": "kept"}}[kind]
    store, cell_id = _stored_cell(tmp_path, kind, stored)

    with pytest.raises(ValueError):
        save_draft(store, cell_id, draft)

    assert store.cell(cell_id)["draft"] == stored


# --- Can run ---------------------------------------------------------------------------------------------------

def test_ai_cells_and_sql_cells_with_something_to_run_can_run():
    for row in (_row("ai", {"prompt": "top ports"}), _row("sql", {"sql": "SELECT 1"}),
                _row("sql", {"sql": "", "command": "SHOW TABLES"}),
                _row("sql", {"sql": "SHOW TABLES", "command": "DESCRIBE TABLE", "table": "flows"}),
                _row("sql", {"sql": "SELECT dport FROM flows WHERE dport IN (SELECT dport FROM flows LIMIT 5)"})):
        assert typed_cell(row, DEFAULT_MODEL).cannot_run() is None, row


def test_an_ai_cell_with_an_empty_prompt_cannot_run():
    for prompt in ("", "  \n\t"):
        assert typed_cell(_row("ai", {"prompt": prompt}), DEFAULT_MODEL).cannot_run() == "Write a prompt to run"


def test_a_sql_cell_with_empty_sql_cannot_run():
    for sql in ("", "  \n"):
        assert typed_cell(_row("sql", {"sql": sql}), DEFAULT_MODEL).cannot_run() == "Write some SQL to run"


def test_a_describe_table_cell_with_no_table_cannot_run():
    cell = typed_cell(_row("sql", {"sql": "", "command": "DESCRIBE TABLE", "table": " "}), DEFAULT_MODEL)

    assert cell.cannot_run() == "Pick a table to describe"


def test_show_or_describe_typed_in_the_sql_box_cannot_run():
    for sql in ("SHOW TABLES", "describe flows", "-- note\nDESC flows"):
        reason = typed_cell(_row("sql", {"sql": sql}), DEFAULT_MODEL).cannot_run()
        assert reason == "SHOW and DESCRIBE are not run from the SQL box: pick one in the Command drop-down instead."


def test_a_query_ending_in_limit_cannot_run_and_is_pointed_to_the_rows_drop_down():
    reason = typed_cell(_row("sql", {"sql": "SELECT dport FROM flows LIMIT 5"}), DEFAULT_MODEL).cannot_run()

    assert reason is not None and reason.startswith("End the query without LIMIT") and "row cap" in reason


def test_a_markdown_cell_is_never_run():
    assert typed_cell(_row("markdown", {"text": "# Notes"}), DEFAULT_MODEL).cannot_run() == "A Markdown Cell is never Run"


# --- Starting Drafts ----------------------------------------------------------------------------------------

EFFORTS = {DEFAULT_MODEL: ModelEfforts(("low", "high"), "low"), "m/other": ModelEfforts(("medium", "max"), "medium"),
           "m/keyless": ModelEfforts(("max",), "max")}


def _can_use(*models):
    return lambda model: model in models


CAN_USE_BOTH = _can_use(DEFAULT_MODEL, "m/other")


def _starting(last, can_use=CAN_USE_BOTH, efforts=EFFORTS):
    return starting_ai_draft(last, can_use, efforts.get, DEFAULT_MODEL)


def test_a_new_ai_cell_with_no_earlier_run_starts_with_the_defaults():
    assert _starting(None) == AiDraft("", AiRunSettings(DEFAULT_MODEL, "low", DEFAULT_MAX_ROWS))


def test_a_new_ai_cell_starts_with_the_last_settings_run_when_that_model_can_be_used():
    assert _starting(AiRunSettings("m/other", "max", 50)) == AiDraft("", AiRunSettings("m/other", "max", 50))


def test_a_new_ai_cell_falls_back_to_the_default_model_when_the_last_model_cannot_be_used():
    assert _starting(AiRunSettings("m/keyless", "max", 50)) == AiDraft("", AiRunSettings(DEFAULT_MODEL, "low", 50))


def test_a_new_ai_cell_falls_back_to_the_models_default_effort_when_the_last_effort_is_not_offered():
    assert _starting(AiRunSettings("m/other", "low", 50)) == AiDraft("", AiRunSettings("m/other", "medium", 50))


def test_a_new_ai_cell_with_a_model_without_effort_control_has_no_effort():
    efforts = {DEFAULT_MODEL: ModelEfforts((), None)}

    assert _starting(None, efforts=efforts) == AiDraft("", AiRunSettings(DEFAULT_MODEL, None, DEFAULT_MAX_ROWS))


def test_a_new_ai_cell_starts_with_the_default_models_default_effort_even_when_that_model_cannot_be_used():
    assert _starting(None, can_use=_can_use()) == AiDraft("", AiRunSettings(DEFAULT_MODEL, "low", DEFAULT_MAX_ROWS))


def test_a_new_sql_cell_starts_with_the_last_row_cap_run():
    assert starting_sql_draft(SqlRunSettings(50, "SHOW TABLES")) == SqlDraft("", SqlRunSettings(50, ""), "")


def test_a_new_sql_cell_with_no_earlier_run_starts_with_the_default_row_cap():
    assert starting_sql_draft(None) == SqlDraft("", SqlRunSettings(DEFAULT_MAX_ROWS, ""), "")
