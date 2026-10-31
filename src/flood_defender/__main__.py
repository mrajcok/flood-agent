"""Run the app: python -m flood_defender

Settings come from the environment, or from a .env file in the working directory
(the real environment wins). See .env.example for every variable.
"""

import os

import uvicorn

from flood_defender.agent import Agent
from flood_defender.config import agent_config_from_env, load_env_file
from flood_defender.llm import OpenRouterClient
from flood_defender.web import create_app


def build_app():
    load_env_file(".env", os.environ)
    config = agent_config_from_env(os.environ)
    config.results_db.parent.mkdir(parents=True, exist_ok=True)
    return create_app(Agent(config, OpenRouterClient.from_env(config.model)))


if __name__ == "__main__":
    uvicorn.run(build_app(), host=os.environ.get("HOST", "127.0.0.1"), port=int(os.environ.get("PORT", "8000")))
