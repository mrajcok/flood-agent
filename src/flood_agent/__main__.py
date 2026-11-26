"""Run the app: python -m flood_agent

Settings come from defaults.toml, a .env file in the working directory, and the
environment, in that order of increasing priority. See .env.example for every setting.
"""

import copy
import logging
import logging.config
from pathlib import Path

import uvicorn
from uvicorn.config import LOGGING_CONFIG

from flood_agent import logs
from flood_agent.agent import Agent
from flood_agent.config import Settings
from flood_agent.fake_llm import CannedPrompt, load_canned_prompts
from flood_agent.web import PickerEntry, create_app


def picker_entries(canned_prompts: list[CannedPrompt]) -> list[PickerEntry]:
    """The page's picker: each top-level Canned Prompt with its follow-ups nested under it."""

    def entry(c: CannedPrompt) -> PickerEntry:
        follow_ups = [entry(f) for f in canned_prompts if f.after == c.id]
        return PickerEntry(c.prompt, c.fails_on_purpose, follow_ups)

    return [entry(c) for c in canned_prompts if c.after is None]


def log_config() -> dict:
    """Uvicorn's terminal logging, with an HH:MM:SS time before each line's INFO:/ERROR: prefix."""
    config = copy.deepcopy(LOGGING_CONFIG)
    for formatter in config["formatters"].values():
        formatter["fmt"] = "%(asctime)s " + formatter["fmt"]
        formatter["datefmt"] = logs.DATE_FORMAT
    return config


def remove_legacy_databases(*databases: Path) -> None:
    """Delete the single-user databases from before Users, where they exist: nothing in them is migrated."""
    for path in databases:
        if path.is_file():
            path.unlink()
            logging.getLogger("uvicorn.error").warning("Removed the single-user database %s", path)


def build_app(settings: Settings | None = None, dev: bool = False):
    settings = settings or Settings()  # pyright: ignore[reportCallIssue]  # pydantic-settings fills fields from env/TOML
    sign_in = settings.sign_in_config(dev)  # first: without a User, nothing is made and the app does not start
    remove_legacy_databases(settings.app_db, settings.results_db)
    picker = picker_entries(load_canned_prompts())
    return create_app(Agent(settings.agent_config(), settings.client_factory()), sign_in, picker=picker, dev=dev)


def main(dev: bool = False) -> None:
    settings = Settings()  # pyright: ignore[reportCallIssue]  # pydantic-settings fills fields from env/TOML
    logging.config.dictConfig(log_config())  # now, so that what startup logs is formatted like the rest
    uvicorn.run(build_app(settings, dev=dev), host=settings.host, port=settings.port, log_config=log_config())


if __name__ == "__main__":
    main()
