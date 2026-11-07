"""Run the app: python -m flood_agent

Settings come from defaults.toml, a .env file in the working directory, and the
environment, in that order of increasing priority. See .env.example for every setting.
"""

import uvicorn

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


def build_app(settings: Settings | None = None):
    settings = settings or Settings()  # pyright: ignore[reportCallIssue]  # pydantic-settings fills fields from env/TOML
    config = settings.agent_config()
    config.results_db.parent.mkdir(parents=True, exist_ok=True)
    picker = picker_entries(load_canned_prompts())
    has_api_key = settings.openrouter_api_key is not None
    return create_app(Agent(config, settings.client_factory()), picker=picker, has_api_key=has_api_key)


if __name__ == "__main__":
    settings = Settings()  # pyright: ignore[reportCallIssue]  # pydantic-settings fills fields from env/TOML
    uvicorn.run(build_app(settings), host=settings.host, port=settings.port)
