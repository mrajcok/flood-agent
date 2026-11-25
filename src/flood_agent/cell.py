"""Cells, typed: an AI Cell, a SQL Cell or a Markdown Cell, each with its typed Draft and (AI and SQL Cells) what
they need of their latest Run.

`typed_cell` builds one from the row Notebook storage returns. Missing settings in old stored rows are filled in
there, once: a missing model becomes the default model, a missing row cap the default row cap. Nothing downstream
defaults again.

Each AI and SQL Cell answers what its Draft would run as: its prompt or statement, and its Run settings.

Each AI and SQL Cell answers whether it is edited: whether its Draft differs from its latest Run.

Each Cell answers `cannot_run()`: why it cannot be Run (a plain, value-free reason), or None if it can.
"""

from collections.abc import Callable
from dataclasses import dataclass, replace

from flood_agent import queries
from flood_agent.cell_kinds import AI, MARKDOWN, SQL
from flood_agent.notebook_store import DEFAULT_MAX_ROWS, NotebookStore
from flood_agent.show_commands import DESCRIBE_TABLE, SHOW_IN_TEXTBOX, is_show_statement, statement_of


def _squash(text: str) -> str:
    """Text with every run of whitespace as one space and none at the ends: spacing alone is not an edit."""
    return " ".join(text.split())


def _differs(text: str, ran: str, settings: object, ran_settings: object) -> bool:
    """Whether a Draft that would run `text` with `settings` differs from a Run of `ran` with `ran_settings`: the
    text compared ignoring spacing, the settings exactly."""
    return _squash(text) != _squash(ran) or settings != ran_settings


@dataclass(frozen=True)
class AiRunSettings:
    """What an AI Cell runs with besides its prompt."""

    model: str
    effort: str | None
    max_rows: int

    @classmethod
    def from_stored(cls, s: dict) -> "AiRunSettings":
        """From the settings a Run recorded (see `stored`)."""
        return cls(s["model"], s.get("effort"), s["max_rows"])

    def stored(self) -> dict:
        """As a Run records them (a stored format: keep its keys)."""
        return {"model": self.model, "effort": self.effort, "max_rows": self.max_rows}


@dataclass(frozen=True)
class AiDraft:
    prompt: str
    settings: AiRunSettings

    def stored(self) -> dict:
        """As Notebook storage keeps it (a stored format: keep its keys)."""
        return {"prompt": self.prompt, **self.settings.stored()}


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
        return self.draft.settings

    @property
    def edited(self) -> bool:
        """Whether its Draft differs from its latest Run: the prompt compared ignoring spacing, the settings exactly.
        Never, with no Run. (The page's live check in cell_ai.html mirrors this.)"""
        return self.run is not None and _differs(self.prompt, self.run.prompt, self.run_settings, self.run.settings)

    def cannot_run(self) -> str | None:
        """Why its Draft cannot be Run (a plain, value-free reason), or None if it can be."""
        return None if self.prompt.strip() else "Write a prompt to run"


@dataclass(frozen=True)
class SqlRunSettings:
    """What a SQL Cell runs with besides its statement. `command` is "" for a query."""

    max_rows: int
    command: str

    @classmethod
    def from_stored(cls, s: dict) -> "SqlRunSettings":
        """From the settings a Run recorded (see `stored`)."""
        return cls(s["max_rows"], s.get("command") or "")

    def stored(self) -> dict:
        """As a Run records them (a stored format: keep its keys)."""
        return {"max_rows": self.max_rows, "command": self.command}


@dataclass(frozen=True)
class SqlDraft:
    """A query in `sql`, or a SHOW or DESCRIBE TABLE command (in its settings, with its `table`), which then runs
    instead."""

    sql: str
    settings: SqlRunSettings
    table: str

    def stored(self) -> dict:
        """As Notebook storage keeps it (a stored format: keep its keys)."""
        return {"sql": self.sql, **self.settings.stored(), "table": self.table}


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
        command = self.draft.settings.command
        return statement_of(command, self.draft.table) if command else self.draft.sql

    @property
    def run_settings(self) -> SqlRunSettings:
        """The settings its Draft would run with."""
        return self.draft.settings

    @property
    def edited(self) -> bool:
        """Whether its Draft differs from its latest Run: the statement compared ignoring spacing, the settings
        exactly. Never, with no Run. (The page's live check in cell_sql.html mirrors this.)"""
        return self.run is not None and _differs(self.statement, self.run.statement, self.run_settings,
                                                 self.run.settings)

    def cannot_run(self) -> str | None:
        """Why its Draft cannot be Run (a plain, value-free reason), or None if it can be. The page's Alpine
        `runnable` getter (cell_sql.html) mirrors some of these rules as the user types."""
        draft, command = self.draft, self.draft.settings.command
        if command:
            return "Pick a table to describe" if command == DESCRIBE_TABLE and not draft.table.strip() else None
        if is_show_statement(draft.sql):
            return SHOW_IN_TEXTBOX
        if queries.ends_with_limit(draft.sql):
            return queries.LIMIT_IN_SQL_CELL
        return None if draft.sql.strip() else "Write some SQL to run"


@dataclass(frozen=True)
class MarkdownDraft:
    """Its text, whether it is sent to the AI Cells below, and its embedded Displays (each {kind, table, spec?})."""

    text: str
    send_to_ai: bool
    displays: tuple[dict, ...]

    def stored(self) -> dict:
        """As Notebook storage keeps it (a stored format: keep its keys)."""
        return {"text": self.text, "send_to_ai": self.send_to_ai, "displays": list(self.displays)}


NEVER_RUN = "A Markdown Cell is never Run"


@dataclass(frozen=True)
class MarkdownCell:
    """Never Run, so it has no Run."""

    id: str
    notebook_id: str
    draft: MarkdownDraft

    def cannot_run(self) -> str | None:
        """Always: a Markdown Cell is never Run."""
        return NEVER_RUN


Cell = AiCell | SqlCell | MarkdownCell


def typed_cell(row: dict, default_model: str) -> Cell:
    """The typed Cell for a row from Notebook storage ({id, notebook_id, kind, draft, run}). `default_model` (from
    config) stands in for a missing model."""
    draft, run = row["draft"], row["run"]
    if row["kind"] == AI:
        settings = AiRunSettings(draft.get("model") or default_model, draft.get("effort"),
                                 draft.get("max_rows") or DEFAULT_MAX_ROWS)
        latest = LatestAiRun(run["id"], run["prompt"], AiRunSettings.from_stored(run["settings"])) if run else None
        return AiCell(row["id"], row["notebook_id"], AiDraft(draft.get("prompt", ""), settings), latest)
    if row["kind"] == SQL:
        settings = SqlRunSettings(draft.get("max_rows") or DEFAULT_MAX_ROWS, draft.get("command") or "")
        latest_sql = LatestSqlRun(run["id"], run["prompt"], SqlRunSettings.from_stored(run["settings"])) if run else None
        return SqlCell(row["id"], row["notebook_id"], SqlDraft(draft.get("sql", ""), settings, draft.get("table") or ""),
                       latest_sql)
    if row["kind"] == MARKDOWN:
        return MarkdownCell(row["id"], row["notebook_id"], MarkdownDraft(
            draft.get("text", ""), bool(draft.get("send_to_ai")), tuple(draft.get("displays") or ())))
    raise ValueError(f"unknown Cell kind {row['kind']!r}")


# --- saving a Draft ---------------------------------------------------------------------------------------------

Draft = AiDraft | SqlDraft | MarkdownDraft

_KIND_OF_DRAFT = {AiDraft: AI, SqlDraft: SQL, MarkdownDraft: MARKDOWN}


def kind_of(draft: Draft) -> str:
    """The Cell kind a Draft is for."""
    return _KIND_OF_DRAFT[type(draft)]


def save_draft(notebooks: NotebookStore, cell_id: str, draft: Draft) -> None:
    """Save `draft` as the Cell's Draft, whole, replacing the stored one (even when it cannot be Run). The latest Run
    is untouched. ValueError if the Draft is not of the Cell's kind; KeyError if the Cell is unknown."""
    notebooks.replace_draft(cell_id, kind_of(draft), draft.stored())


# --- Can run ---------------------------------------------------------------------------------------------------


class CannotRun(Exception):
    """The Cell cannot be Run; the message is the plain, value-free reason (see each Cell's `cannot_run`)."""


# --- Starting Drafts --------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class ModelEfforts:
    """The Efforts a model offers (from the curated model list) and its default Effort."""

    efforts: tuple[str, ...]
    default_effort: str | None


def starting_ai_draft(
    last: AiRunSettings | None, can_use: Callable[[str], bool], efforts_of: Callable[[str], ModelEfforts | None],
    default_model: str,
) -> AiDraft:
    """What a new AI Cell starts with, given the settings last Run for an AI Cell (None: no earlier Run): those
    settings if that model `can_use` here (e.g. it has an API key), else the default model. Its Effort is the last
    one if the model offers it (`efforts_of`, None when nothing is known of the model), else the model's default."""
    if last is None:
        last = AiRunSettings(default_model, None, DEFAULT_MAX_ROWS)
    elif not can_use(last.model):
        last = AiRunSettings(default_model, None, last.max_rows)
    known = efforts_of(last.model)
    effort = None if known is None else last.effort if last.effort in known.efforts else known.default_effort
    return AiDraft("", replace(last, effort=effort))


def starting_sql_draft(last: SqlRunSettings | None) -> SqlDraft:
    """What a new SQL Cell starts with, given the settings last Run for a SQL Cell (None: no earlier Run): an empty
    query with that Run's row cap."""
    return SqlDraft("", SqlRunSettings(last.max_rows if last else DEFAULT_MAX_ROWS, ""), "")
