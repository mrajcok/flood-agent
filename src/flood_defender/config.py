"""Settings, layered from defaults.toml, then a .env file, then the real environment.

The Conversation id is deliberately not a setting: the Agent sends it with every MCP tool
call (in request `_meta`), and it scopes which Result Tables a call may use.
"""

import tomllib
from collections.abc import Mapping
from pathlib import Path
from typing import Literal

from dotenv import dotenv_values
from pydantic import Field, SecretStr
from pydantic_settings import (
    BaseSettings,
    DotEnvSettingsSource,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
    TomlConfigSettingsSource,
)

from flood_defender.agent import AgentConfig

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

    llm_backend: Literal["openrouter", "fake"] = Field(
        description="openrouter calls a real model (needs OPENROUTER_API_KEY). fake is Fake Mode: a scripted\n"
        "stand-in answers Canned Prompts listed on the page, so no model is called and no key is needed.\n"
        "Try it for one run with: LLM_BACKEND=fake uv run python -m flood_defender"
    )
    openrouter_api_key: SecretStr | None = Field(
        None, description="OpenRouter API key. Required when LLM_BACKEND is openrouter; the only setting without a default. Never commit a real key."
    )
    openrouter_model: str = Field(
        description="Model used for chat.\n"
        "Cheaper open-weight models to try (confirm with the eval runner first):\n"
        "deepseek/deepseek-v4-pro-0813, z-ai/glm-5.3"
    )
    telemetry_db: Path = Field(
        description="Read-only flow records. Create with: uv run python -m flood_defender.generator data/telemetry.db"
    )
    results_db: Path = Field(description="Writable database holding Result Tables.")
    app_db: Path = Field(
        description="Conversations, messages, Displays and Traces.\n"
        "Tip: use a separate database for Fake Mode, for example data/fake.db."
    )
    semantic_layer: Path = Field(
        description="Hand-authored Semantic Layer (the generator also writes window_start/window_end into it)."
    )
    result_ttl_hours: float = Field(
        gt=0, description="Result Tables are deleted this many hours after their Conversation's last activity."
    )
    query_timeout_s: float = Field(gt=0, description="A query running longer than this many seconds is aborted.")
    max_result_rows: int = Field(
        ge=1, description="A query producing more rows than this is refused (the LLM is told to aggregate or filter)."
    )
    chart_row_cap: int = Field(ge=1, description="A chart over a Result Table with more rows than this is refused.")
    max_failed_attempts: int = Field(
        ge=1, description="Failed run_query attempts allowed per user turn before the Agent gives up."
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

    def require_api_key(self) -> str:
        if self.openrouter_api_key is None:
            raise RuntimeError("OPENROUTER_API_KEY is not set")
        return self.openrouter_api_key.get_secret_value()

    def agent_config(self) -> AgentConfig:
        return AgentConfig(
            telemetry_db=self.telemetry_db.resolve(),
            results_db=self.results_db.resolve(),
            semantic_layer=self.semantic_layer.resolve(),
            app_db=self.app_db.resolve(),
            model="fake" if self.llm_backend == "fake" else self.openrouter_model,
            result_ttl_ms=int(self.result_ttl_hours * 3600 * 1000),
            query_timeout_s=self.query_timeout_s,
            max_result_rows=self.max_result_rows,
            chart_row_cap=self.chart_row_cap,
            max_failed_attempts=self.max_failed_attempts,
            max_llm_steps=self.max_llm_steps,
        )


_ENV_EXAMPLE_HEADER = """\
# Flood Defender settings.
#
# Generated from flood_defender/config.py and flood_defender/defaults.toml; regenerate with:
#   uv run python -m flood_defender.config > .env.example
#
# Copy to .env (already gitignored). The app reads .env from the working directory at
# startup. A variable already set in the real environment wins over the same variable in
# .env, and blank values count as unset.
#
# Only OPENROUTER_API_KEY needs a value. The other lines are commented out and show the
# default from defaults.toml; uncomment one only to change it, so a copied file doesn't
# pin defaults that may change later."""

_ENV_EXAMPLE_FOOTER = """\
# The eval runner (python -m flood_defender.evalset)
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
        lines.append(f"# {variable}={defaults[name]}" if name in defaults else f"{variable}=")
    lines += ["", _ENV_EXAMPLE_FOOTER]
    return "\n".join(lines) + "\n"


if __name__ == "__main__":
    print(render_env_example(), end="")
