"""Settings, layered from defaults.toml, then a .env file, then the real environment.

The row cap is deliberately not a setting here: the Agent sends the AI Cell's with every MCP tool
call (in request `_meta`).
"""

import tomllib
from collections.abc import Mapping
from pathlib import Path

from dotenv import dotenv_values
from pydantic import Field, SecretStr, model_validator
from pydantic_settings import (
    BaseSettings,
    DotEnvSettingsSource,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
    TomlConfigSettingsSource,
)

from flood_agent.agent import NO_MODEL, AgentConfig, ClientFactory
from flood_agent.fake_llm import FakeClient, load_canned_prompts
from flood_agent.llm import LLMClient, ModelSpec, OpenRouterClient

DEFAULTS_FILE = Path(__file__).with_name("defaults.toml")


class _LiteralDotEnvSource(DotEnvSettingsSource):
    """The stock .env source expands `$VAR` and `${VAR}`, which silently rewrites keys and paths.

    This overrides a private method of pydantic-settings. If an upgrade stops it from being
    used, test_dollar_signs_in_env_file_values_are_kept_literally fails."""

    def _read_env_file(self, file_path: Path) -> Mapping[str, str | None]:
        values = dotenv_values(file_path, encoding=self.env_file_encoding or "utf-8", interpolate=False)
        return {
            (key if self.case_sensitive else key.lower()): value
            for key, value in values.items()
            if value is not None and not (self.env_ignore_empty and value == "")
        }


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_ignore_empty=True, extra="ignore", toml_file=DEFAULTS_FILE
    )

    openrouter_api_key: SecretStr | None = Field(
        None, description="OpenRouter API key. Needed to send a prompt to a real model (not No model); the only setting without a default. Never commit a real key."
    )
    default_model: str = Field(
        description="The model preselected under the prompt box: an id from the curated [[models]] list in\n"
        "defaults.toml. The list (label, context window, Effort levels) is edited there, not here."
    )
    models: list[ModelSpec] = Field(
        description="MODELS: the curated model list, the [[models]] tables in defaults.toml (not set from .env)."
    )
    telemetry_db: Path = Field(
        description="Read-only flow records. Create with: uv run python -m flood_agent.generator data/telemetry.db"
    )
    results_db: Path = Field(description="Writable database holding Result Tables.")
    app_db: Path = Field(
        description="Notebooks, Cells, Runs, Displays and Traces, and the table registry."
    )
    semantic_layer: Path = Field(
        description="Hand-authored Semantic Layer."
    )
    result_ttl_hours: float = Field(
        gt=0, description="Result Tables are deleted this many hours after they are created."
    )
    query_timeout_s: float = Field(gt=0, description="A query running longer than this many seconds is aborted.")
    chart_row_cap: int = Field(ge=1, description="A chart over a Result Table with more rows than this shows only its first rows.")
    max_failed_attempts: int = Field(
        ge=1, description="Failed run_query / run_count_query attempts allowed per user turn before the Agent gives up."
    )
    max_llm_steps: int = Field(
        ge=1, description="LLM calls allowed per user turn (a backstop against a model looping on tool calls)."
    )
    host: str = Field(description="Address the web server listens on.")
    port: int = Field(ge=1, le=65535, description="Port the web server listens on.")

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        assert isinstance(dotenv_settings, DotEnvSettingsSource)
        literal_dotenv = _LiteralDotEnvSource(settings_cls, env_file=dotenv_settings.env_file)
        return (init_settings, env_settings, literal_dotenv, TomlConfigSettingsSource(settings_cls))

    @classmethod
    def from_defaults(cls) -> "Settings":
        """Only defaults.toml, ignoring any .env file or environment variable. For reproducible runs."""
        return cls.model_validate(tomllib.loads(DEFAULTS_FILE.read_text()))

    @model_validator(mode="after")
    def _default_model_is_listed(self) -> "Settings":
        if self.default_model not in {m.id for m in self.models}:
            raise ValueError(f"default_model {self.default_model!r} is not an id in the [[models]] list")
        return self

    def require_api_key(self) -> str:
        if self.openrouter_api_key is None:
            raise RuntimeError("OPENROUTER_API_KEY is not set")
        return self.openrouter_api_key.get_secret_value()

    def client_factory(self) -> ClientFactory:
        """The client per model id, made on first use (so the app starts without a key) and then
        reused: the scripted stand-in for NO_MODEL (no key needed), else the real OpenRouter client,
        which raises RuntimeError naming OPENROUTER_API_KEY when there is no key."""
        clients: dict[str, LLMClient] = {}

        def client_for(model: str) -> LLMClient:
            if model not in clients:
                if model == NO_MODEL:
                    clients[model] = FakeClient(load_canned_prompts(), telemetry_db=self.telemetry_db)
                else:
                    clients[model] = OpenRouterClient(self.require_api_key(), model)
            return clients[model]

        return client_for

    def agent_config(self, model: str | None = None) -> AgentConfig:
        """`model` is the default model for a turn that names none; it defaults to `default_model`."""
        return AgentConfig(
            telemetry_db=self.telemetry_db.resolve(),
            results_db=self.results_db.resolve(),
            semantic_layer=self.semantic_layer.resolve(),
            app_db=self.app_db.resolve(),
            model=model or self.default_model,
            models=tuple(self.models),
            result_ttl_ms=int(self.result_ttl_hours * 3600 * 1000),
            query_timeout_s=self.query_timeout_s,
            chart_row_cap=self.chart_row_cap,
            max_failed_attempts=self.max_failed_attempts,
            max_llm_steps=self.max_llm_steps,
        )


_ENV_EXAMPLE_HEADER = """\
# CANOPY settings.
#
# Generated from flood_agent/config.py and flood_agent/defaults.toml; regenerate with:
#   uv run python -m flood_agent.config > .env.example
#
# Copy to .env (already gitignored). The app reads .env from the working directory at
# startup. A variable already set in the real environment wins over the same variable in
# .env, and blank values count as unset.
#
# Only OPENROUTER_API_KEY needs a value. The other lines are commented out and show the
# default from defaults.toml; uncomment one only to change it, so a copied file doesn't
# pin defaults that may change later."""

_ENV_EXAMPLE_FOOTER = """\
# The eval runner (python -m flood_agent.evalset)
# reads OPENROUTER_API_KEY and the telemetry_db and semantic_layer paths (overridable with
# --db and --semantic-layer) from this file or the environment, takes its model list from
# --models, and always uses the limits in defaults.toml so runs are comparable."""


def render_env_example() -> str:
    defaults = tomllib.loads(DEFAULTS_FILE.read_text())
    lines = [_ENV_EXAMPLE_HEADER]
    for name, field in Settings.model_fields.items():
        lines.append("")
        lines += [f"# {line}" for line in (field.description or "").splitlines()]
        variable = name.upper()
        if name == "models":
            continue  # a list of tables: edited in defaults.toml, never set from .env
        lines.append(f"# {variable}={defaults[name]}" if name in defaults else f"{variable}=")
    lines += ["", _ENV_EXAMPLE_FOOTER]
    return "\n".join(lines) + "\n"


if __name__ == "__main__":
    print(render_env_example(), end="")
