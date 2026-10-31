"""Run the app: python -m flood_defender

Environment: OPENROUTER_API_KEY (required), OPENROUTER_MODEL (default
anthropic/claude-sonnet-5.5), TELEMETRY_DB, RESULTS_DB, SEMANTIC_LAYER, RESULT_TTL_HOURS (default 24), HOST, PORT.
"""

import os
from pathlib import Path

import uvicorn

from flood_defender.agent import Agent, AgentConfig
from flood_defender.llm import OpenRouterClient, model_from_env
from flood_defender.web import create_app


def build_app():
    config = AgentConfig(
        telemetry_db=Path(os.environ.get("TELEMETRY_DB", "data/telemetry.db")).resolve(),
        results_db=Path(os.environ.get("RESULTS_DB", "data/results.db")).resolve(),
        semantic_layer=Path(os.environ.get("SEMANTIC_LAYER", "semantic_layer.yaml")),
        app_db=Path(os.environ.get("APP_DB", "data/app.db")).resolve(),
        model=model_from_env(),
        result_ttl_ms=int(float(os.environ.get("RESULT_TTL_HOURS", "24")) * 3600 * 1000),
    )
    config.results_db.parent.mkdir(parents=True, exist_ok=True)
    return create_app(Agent(config, OpenRouterClient.from_env()))


if __name__ == "__main__":
    uvicorn.run(build_app(), host=os.environ.get("HOST", "127.0.0.1"), port=int(os.environ.get("PORT", "8000")))
