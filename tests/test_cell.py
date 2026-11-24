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
