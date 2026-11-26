"""Eval harness helpers: the harness's own User and, over their Notebook module, cells above a question and a question as
an AI Cell."""

from collections.abc import AsyncIterator
from dataclasses import replace
from pathlib import Path

from flood_agent.agent import AgentConfig, UserSpace
from flood_agent.cell import AiDraft, AiRunSettings, MarkdownDraft, SqlDraft, SqlRunSettings
from flood_agent.cell_kinds import AI, MARKDOWN, SQL
from flood_agent.config import Settings
from flood_agent.events import Event
from flood_agent.model_choices import ModelChoices
from flood_agent.notebook_store import DEFAULT_MAX_ROWS
from flood_agent.users import user_databases

EVAL_USER = "eval"


def eval_model_choices(settings: Settings, model: str) -> ModelChoices:
    """An eval brings its own client for `model`, so `model` is the default (for its Cells, which name none) and can
    be used, whether or not an API key is set."""
    return settings.model_choices(model, has_api_key=True)


def eval_user_config(config: AgentConfig, users_dir: Path) -> AgentConfig:
    """`config` for the harness's User (`EVAL_USER`) alone, who never signs in, with new, empty databases under
    `users_dir`."""
    databases = user_databases(users_dir, EVAL_USER)
    for path in (databases.app_db, databases.results_db):
        path.unlink(missing_ok=True)
    return replace(config, users_dir=users_dir, users=(EVAL_USER,))


async def add_cells_above(user: UserSpace, notebook_id: str, above: list[dict] | None) -> None:
    """Append the cells a golden question has above it, top to bottom. SQL and SHOW cells are run, so that the
    AI Cell below is sent what they ran; Markdown cells are only added (a ticked one is sent)."""
    notebooks = user.notebooks
    for spec in above or []:
        kind = spec["kind"]
        if kind == "markdown":
            notebooks.add_cell(notebook_id, MARKDOWN, draft=MarkdownDraft(spec["text"], bool(spec.get("send_to_ai")), ()))
            continue
        if kind == "sql":
            draft = SqlDraft(spec["sql"], SqlRunSettings(DEFAULT_MAX_ROWS, ""), "")
        elif kind == "show":
            draft = SqlDraft("", SqlRunSettings(DEFAULT_MAX_ROWS, spec["command"]), spec.get("table", ""))
        else:
            raise ValueError(f"unknown cell kind {kind!r}")
        cell_id = notebooks.add_cell(notebook_id, SQL, draft=draft)
        async for _ in notebooks.run_cell(notebook_id, cell_id):
            pass


async def run_ai_cell(user: UserSpace, notebook_id: str, prompt: str, max_rows: int) -> tuple[str, AsyncIterator[Event]]:
    """Add an AI Cell at the end of the notebook, with the default model, and start running it: (its id, its
    events)."""
    notebooks = user.notebooks
    settings = AiRunSettings(user.config.choices.default_model, None, max_rows)
    cell_id = notebooks.add_cell(notebook_id, AI, draft=AiDraft(prompt, settings))
    return cell_id, notebooks.run_cell(notebook_id, cell_id)
