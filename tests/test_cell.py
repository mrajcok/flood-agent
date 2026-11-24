"""The Cell module, tested by passing the rows Notebook storage returns: no HTTP client, no MCP server."""

from flood_agent.cell import (
    AiCell,
    AiDraft,
    AiRunSettings,
    LatestAiRun,
    LatestSqlRun,
    MarkdownCell,
    MarkdownDraft,
    SqlCell,
    SqlDraft,
    SqlRunSettings,
    UsableModel,
    starting_ai_draft,
    starting_sql_draft,
    typed_cell,
)
from flood_agent.notebooks import DEFAULT_MAX_ROWS

DEFAULT_MODEL = "default/model"


def _row(kind, draft, run=None, id="c1", notebook_id="nb1"):
    return {"id": id, "notebook_id": notebook_id, "kind": kind, "draft": draft, "run": run}


def test_an_ai_cell_is_built_with_its_draft():
    cell = typed_cell(_row("ai", {"prompt": "top ports", "model": "m/x", "effort": "high", "max_rows": 50}),
                      DEFAULT_MODEL)

    assert cell == AiCell("c1", "nb1", AiDraft("top ports", "m/x", "high", 50), None)
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

    assert cell == SqlCell("c1", "nb1", SqlDraft("SELECT 1", 20, "", ""), None)
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


# --- Starting Drafts ----------------------------------------------------------------------------------------

USABLE = {DEFAULT_MODEL: UsableModel(("low", "high"), "low"), "m/other": UsableModel(("medium", "max"), "medium")}


def test_a_new_ai_cell_with_no_earlier_run_starts_with_the_defaults():
    assert starting_ai_draft(None, USABLE.get, DEFAULT_MODEL) == AiDraft("", DEFAULT_MODEL, "low", DEFAULT_MAX_ROWS)


def test_a_new_ai_cell_starts_with_the_last_settings_run_when_that_model_can_be_used():
    last = {"model": "m/other", "effort": "max", "max_rows": 50}

    assert starting_ai_draft(last, USABLE.get, DEFAULT_MODEL) == AiDraft("", "m/other", "max", 50)


def test_a_new_ai_cell_falls_back_to_the_default_model_when_the_last_model_cannot_be_used():
    last = {"model": "m/keyless", "effort": "max", "max_rows": 50}

    assert starting_ai_draft(last, USABLE.get, DEFAULT_MODEL) == AiDraft("", DEFAULT_MODEL, "low", 50)


def test_a_new_ai_cell_falls_back_to_the_models_default_effort_when_the_last_effort_is_not_offered():
    last = {"model": "m/other", "effort": "low", "max_rows": 50}

    assert starting_ai_draft(last, USABLE.get, DEFAULT_MODEL) == AiDraft("", "m/other", "medium", 50)


def test_a_new_ai_cell_with_a_model_without_effort_control_has_no_effort():
    usable = {DEFAULT_MODEL: UsableModel((), None)}.get

    assert starting_ai_draft(None, usable, DEFAULT_MODEL) == AiDraft("", DEFAULT_MODEL, None, DEFAULT_MAX_ROWS)


def test_a_new_sql_cell_starts_with_the_last_row_cap_run():
    assert starting_sql_draft({"max_rows": 50, "command": "SHOW TABLES"}) == SqlDraft("", 50, "", "")


def test_a_new_sql_cell_with_no_earlier_run_starts_with_the_default_row_cap():
    assert starting_sql_draft(None) == SqlDraft("", DEFAULT_MAX_ROWS, "", "")


# --- Edited, not run (#181) -------------------------------------------------------------------------------------

AI_RUN = {"id": "r1", "status": "ok", "prompt": "top ports", "settings": {"model": "m/x", "effort": "high", "max_rows": 50}}


def _ai(prompt="top ports", model="m/x", effort="high", max_rows=50, run=None):
    return typed_cell(_row("ai", {"prompt": prompt, "model": model, "effort": effort, "max_rows": max_rows}, run),
                      DEFAULT_MODEL)


def test_an_ai_cell_never_run_is_not_edited():
    assert not _ai().edited


def test_an_ai_cell_whose_draft_is_what_it_ran_is_not_edited():
    assert not _ai(run=AI_RUN).edited


def test_an_ai_cell_differing_from_its_run_only_in_spacing_is_not_edited():
    assert not _ai(prompt="  top\n\n   ports\t", run=AI_RUN).edited


def test_an_ai_cell_with_a_changed_prompt_is_edited():
    assert _ai(prompt="top hosts", run=AI_RUN).edited


def test_an_ai_cell_with_a_changed_model_effort_or_row_cap_is_edited():
    assert _ai(model="m/y", run=AI_RUN).edited
    assert _ai(effort="low", run=AI_RUN).edited
    assert _ai(effort=None, run=AI_RUN).edited
    assert _ai(max_rows=51, run=AI_RUN).edited


def _sql(sql="SELECT 1", max_rows=20, command="", table="", run=None):
    return typed_cell(_row("sql", {"sql": sql, "max_rows": max_rows, "command": command, "table": table}, run),
                      DEFAULT_MODEL)


def _sql_run(statement="SELECT 1", max_rows=20, command=""):
    return {"id": "r1", "status": "ok", "prompt": statement, "settings": {"max_rows": max_rows, "command": command}}


def test_a_sql_cell_never_run_is_not_edited():
    assert not _sql().edited


def test_a_sql_cell_whose_draft_is_what_it_ran_is_not_edited():
    assert not _sql(run=_sql_run()).edited
    assert not _sql(sql="", command="DESCRIBE TABLE", table="flows",
                    run=_sql_run("DESCRIBE TABLE flows", command="DESCRIBE TABLE")).edited


def test_a_sql_cell_differing_from_its_run_only_in_spacing_is_not_edited():
    assert not _sql(sql="SELECT\n    1  ", run=_sql_run("  SELECT 1")).edited


def test_a_sql_cell_with_a_changed_statement_is_edited():
    assert _sql(sql="SELECT 2", run=_sql_run()).edited


def test_a_sql_cell_with_a_changed_command_described_table_or_row_cap_is_edited():
    assert _sql(command="SHOW TABLES", run=_sql_run()).edited
    assert _sql(sql="", command="DESCRIBE TABLE", table="hosts",
                run=_sql_run("DESCRIBE TABLE flows", command="DESCRIBE TABLE")).edited
    assert _sql(max_rows=21, run=_sql_run()).edited
