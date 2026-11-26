"""Settings, layered from defaults.toml, then a .env file, then the real environment.

The row cap is deliberately not a setting here: the Agent sends the AI Cell's with every MCP tool
call (in request `_meta`, with the User the call is for).
"""

import tomllib
from collections.abc import Mapping
from pathlib import Path

from dotenv import dotenv_values
from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import (
    BaseSettings,
    DotEnvSettingsSource,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
    TomlConfigSettingsSource,
)

from flood_agent.agent import AgentConfig
from flood_agent.ai_cell_run import ClientFactory
from flood_agent.fake_llm import FakeClient, load_canned_prompts
from flood_agent.llm import LLMClient, ModelSpec, OpenRouterClient
from flood_agent.model_choices import NO_MODEL, ModelChoices
from flood_agent.sign_in import SignInConfig
from flood_agent.users import parse_users

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
        env_file=".env", env_ignore_empty=True, extra="ignore", toml_file=DEFAULTS_FILE,
        hide_input_in_errors=True,  # settings hold secrets, which a startup error must not print
    )

    openrouter_api_key: SecretStr | None = Field(
        None, description="OpenRouter API key. Needed to send a prompt to a real model (not No model); has no default. Never commit a real key."
    )
    users: SecretStr | None = Field(
        None, description="The Users who can sign in, as name:hash,name:hash; the app does not start without one, and it has no default.\n"
        "Make each entry with: uv run flood-agent-user NAME"
    )
    default_model: str = Field(
        description="The model preselected under the prompt box: an id from the curated [[models]] list in\n"
        "defaults.toml. The list (label, context window, Effort levels) is edited there, not here."
    )
    models: list[ModelSpec] = Field(
        description="MODELS: the curated model list, the [[models]] tables in defaults.toml (not set from .env)."
    )
    telemetry_db: Path = Field(
        description="Read-only network telemetry (flow records, DNS response metadata and Diameter signaling). Create with: uv run python -m flood_agent.generator data/telemetry.db"
    )
    results_db: Path = Field(
        description="The single-user results database from before Users. Not used: the app deletes this file when it starts."
    )
    app_db: Path = Field(
        description="The single-user app database from before Users. Not used: the app deletes this file when it starts."
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
    users_dir: Path = Field(
        description="Directory holding each User's own databases, created on first use: NAME/app.db (Notebooks, Cells, Runs,\n"
        "Displays, Traces and the table registry) and NAME/results.db (their tables)."
    )
    session_idle_timeout_hours: float = Field(
        gt=0, description="A signed-in session ends after this many hours without a request."
    )
    session_cookie_secure: bool = Field(
        description="Send the session cookie over HTTPS only. flood-agent-dev always turns this off."
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

    @field_validator("users")
    @classmethod
    def _users_are_well_formed(cls, users: SecretStr | None) -> SecretStr | None:
        if users is not None:
            parse_users(users.get_secret_value())
        return users

    @model_validator(mode="after")
    def _default_model_is_listed(self) -> "Settings":
        if self.default_model not in {m.id for m in self.models}:
            raise ValueError(f"default_model {self.default_model!r} is not an id in the [[models]] list")
        return self

    def require_api_key(self) -> str:
        if self.openrouter_api_key is None:
            raise RuntimeError("OPENROUTER_API_KEY is not set")
        return self.openrouter_api_key.get_secret_value()

    def sign_in_config(self, dev: bool = False) -> SignInConfig:
        """Who can sign in and how. RuntimeError naming USERS when no User is configured. `dev`: the dev server, which
        is plain http, so its session cookie is not Secure."""
        if self.users is None:
            raise RuntimeError("USERS is not set: add USERS=name:hash to .env (make one with: uv run flood-agent-user NAME)")
        return SignInConfig(
            users=parse_users(self.users.get_secret_value()),
            idle_timeout_ms=int(self.session_idle_timeout_hours * 3600 * 1000),
            cookie_secure=self.session_cookie_secure and not dev,
        )

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

    def model_choices(self, default_model: str | None = None, has_api_key: bool | None = None) -> ModelChoices:
        """The models that can be used here: the curated list, usable with an API key. The default model is
        `default_model`, or No model when there is no API key. Either can be given instead of the settings' own (an
        eval brings its own client; tests)."""
        key = self.openrouter_api_key is not None if has_api_key is None else has_api_key
        return ModelChoices.build(tuple(self.models), default_model or self.default_model, key)

    def agent_config(self) -> AgentConfig:
        return AgentConfig(
            telemetry_db=self.telemetry_db.resolve(),
            users_dir=self.users_dir.resolve(),
            users=tuple(parse_users(self.users.get_secret_value())) if self.users else (),
            semantic_layer=self.semantic_layer.resolve(),
            choices=self.model_choices(),
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
# Only USERS (to start the app) and OPENROUTER_API_KEY (to use a real model) need a value.
# The other lines are commented out and show the default from defaults.toml; uncomment one
# only to change it, so a copied file doesn't pin defaults that may change later."""

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
