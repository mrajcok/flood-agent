"""Run the app: python -m flood_defender

Settings come from defaults.toml, a .env file in the working directory, and the
environment, in that order of increasing priority. See .env.example for every setting.
"""

import uvicorn

from flood_defender.agent import Agent
from flood_defender.config import Settings
from flood_defender.llm import OpenRouterClient
from flood_defender.web import create_app


def build_app(settings: Settings | None = None):
    settings = settings or Settings()
    config = settings.agent_config()
    config.results_db.parent.mkdir(parents=True, exist_ok=True)
    return create_app(Agent(config, OpenRouterClient(settings.require_api_key(), config.model)))


if __name__ == "__main__":
    settings = Settings()
    uvicorn.run(build_app(settings), host=settings.host, port=settings.port)
