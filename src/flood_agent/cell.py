"""Cells, typed: an AI Cell, a SQL Cell or a Markdown Cell, each with its typed Draft and (AI and SQL Cells) what
they need of their latest Run.

`typed_cell` builds one from the row Notebook storage returns. Missing settings in old stored rows are filled in
there, once: a missing model becomes the default model, a missing row cap the default row cap. Nothing downstream
defaults again.

Each AI and SQL Cell answers what its Draft would run as: its prompt or statement, and its Run settings.
"""

from dataclasses import dataclass

from flood_agent.cell_kinds import AI, MARKDOWN, SQL
from flood_agent.notebooks import DEFAULT_MAX_ROWS
from flood_agent.show_commands import statement_of


@dataclass(frozen=True)
class AiRunSettings:
    """What an AI Cell runs with besides its prompt."""

    model: str
    effort: str | None
    max_rows: int

    def stored(self) -> dict:
        """As a Run records them (a stored format: keep its keys)."""
        return {"model": self.model, "effort": self.effort, "max_rows": self.max_rows}


@dataclass(frozen=True)
class AiDraft:
    prompt: str
    model: str
    effort: str | None
    max_rows: int


@dataclass(frozen=True)
class LatestAiRun:
    """What an AI Cell needs of its latest Run: its id, the prompt it ran and its settings."""

    id: str
    prompt: str
    settings: AiRunSettings


@dataclass(frozen=True)
class AiCell:
    id: str
    notebook_id: str
    draft: AiDraft
    run: LatestAiRun | None

    @property
    def prompt(self) -> str:
        """The prompt its Draft would run."""
        return self.draft.prompt

    @property
    def run_settings(self) -> AiRunSettings:
        """The settings its Draft would run with."""
        return AiRunSettings(self.draft.model, self.draft.effort, self.draft.max_rows)


@dataclass(frozen=True)
class SqlRunSettings:
    """What a SQL Cell runs with besides its statement. `command` is "" for a query."""

    max_rows: int
    command: str

    def stored(self) -> dict:
        """As a Run records them (a stored format: keep its keys)."""
        return {"max_rows": self.max_rows, "command": self.command}


@dataclass(frozen=True)
class SqlDraft:
    """A query in `sql`, or a SHOW or DESCRIBE TABLE `command` (with its `table`), which then runs instead."""

    sql: str
    max_rows: int
    command: str
    table: str


@dataclass(frozen=True)
class LatestSqlRun:
    """What a SQL Cell needs of its latest Run: its id, the statement it ran and its settings."""

    id: str
    statement: str
    settings: SqlRunSettings


@dataclass(frozen=True)
class SqlCell:
    id: str
    notebook_id: str
    draft: SqlDraft
    run: LatestSqlRun | None

    @property
    def statement(self) -> str:
        """The statement its Draft would run: its command (with its table, for DESCRIBE TABLE), else its query."""
        return statement_of(self.draft.command, self.draft.table) if self.draft.command else self.draft.sql

    @property
    def run_settings(self) -> SqlRunSettings:
        """The settings its Draft would run with."""
        return SqlRunSettings(self.draft.max_rows, self.draft.command)


@dataclass(frozen=True)
class MarkdownDraft:
    """Its text, whether it is sent to the AI Cells below, and its embedded Displays (each {kind, table, spec?})."""

    text: str
    send_to_ai: bool
    displays: tuple[dict, ...]


@dataclass(frozen=True)
class MarkdownCell:
    """Never Run, so it has no Run."""

    id: str
    notebook_id: str
    draft: MarkdownDraft


Cell = AiCell | SqlCell | MarkdownCell


def _latest_ai_run(run: dict) -> LatestAiRun:
    s = run["settings"]  # a Run always recorded the settings it ran with
    return LatestAiRun(run["id"], run["prompt"], AiRunSettings(s["model"], s.get("effort"), s["max_rows"]))


def _latest_sql_run(run: dict) -> LatestSqlRun:
    s = run["settings"]
    return LatestSqlRun(run["id"], run["prompt"], SqlRunSettings(s["max_rows"], s.get("command") or ""))


def typed_cell(row: dict, default_model: str) -> Cell:
    """The typed Cell for a row from Notebook storage ({id, notebook_id, kind, draft, run}). `default_model` (from
    config) stands in for a missing model."""
    draft, run = row["draft"], row["run"]
    if row["kind"] == AI:
        return AiCell(row["id"], row["notebook_id"], AiDraft(
            draft.get("prompt", ""), draft.get("model") or default_model, draft.get("effort"),
            draft.get("max_rows") or DEFAULT_MAX_ROWS), _latest_ai_run(run) if run else None)
    if row["kind"] == SQL:
        return SqlCell(row["id"], row["notebook_id"], SqlDraft(
            draft.get("sql", ""), draft.get("max_rows") or DEFAULT_MAX_ROWS, draft.get("command") or "",
            draft.get("table") or ""), _latest_sql_run(run) if run else None)
    if row["kind"] == MARKDOWN:
        return MarkdownCell(row["id"], row["notebook_id"], MarkdownDraft(
            draft.get("text", ""), bool(draft.get("send_to_ai")), tuple(draft.get("displays") or ())))
    raise ValueError(f"unknown Cell kind {row['kind']!r}")
