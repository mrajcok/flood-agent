"""Configuration from a .env file and the process environment.

The real environment wins over the .env file. CONVERSATION_ID is deliberately not
read here: the Agent sets it per turn for the MCP server, and it scopes which Result
Tables a query may read.
"""

from collections.abc import Mapping, MutableMapping
from pathlib import Path

from dotenv import dotenv_values

from flood_defender.agent import AgentConfig
from flood_defender.llm import DEFAULT_MODEL


def load_env_file(path: Path | str, environ: MutableMapping[str, str]) -> None:
    """Add the values in the .env file to `environ`, without overriding what is already set.

    Blank values count as unset, and `$` is never interpolated, so keys and paths stay literal."""
    for key, value in dotenv_values(path, interpolate=False).items():
        if value is not None and value.strip():
            environ.setdefault(key, value)


def agent_config_from_env(environ: Mapping[str, str]) -> AgentConfig:
    optional: dict = {}
    if "QUERY_TIMEOUT_S" in environ:
        optional["query_timeout_s"] = float(environ["QUERY_TIMEOUT_S"])
    if "MAX_RESULT_ROWS" in environ:
        optional["max_result_rows"] = int(environ["MAX_RESULT_ROWS"])
    if "CHART_ROW_CAP" in environ:
        optional["chart_row_cap"] = int(environ["CHART_ROW_CAP"])
    if "RESULT_TTL_HOURS" in environ:
        optional["result_ttl_ms"] = int(float(environ["RESULT_TTL_HOURS"]) * 3600 * 1000)
    return AgentConfig(
        telemetry_db=Path(environ.get("TELEMETRY_DB", "data/telemetry.db")).resolve(),
        results_db=Path(environ.get("RESULTS_DB", "data/results.db")).resolve(),
        semantic_layer=Path(environ.get("SEMANTIC_LAYER", "semantic_layer.yaml")),
        app_db=Path(environ.get("APP_DB", "data/app.db")).resolve(),
        model=environ.get("OPENROUTER_MODEL") or DEFAULT_MODEL,
        **optional,
    )
