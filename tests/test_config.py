"""Settings: defaults.toml, then .env, then the real environment."""

import tomllib
from pathlib import Path

import pytest
from conftest import ScriptedLLM, collect, tool_call
from pydantic import ValidationError

from flood_agent.agent import STEP_LIMIT_MESSAGE, Agent, TextDelta
from flood_agent.config import DEFAULTS_FILE, Settings, render_env_example
from flood_agent.llm import TextChunk

ENV_EXAMPLE = Path(__file__).parent.parent / ".env.example"


def write_env(path, **values):
    path.write_text("".join(f"{k}={v}\n" for k, v in values.items()))
    return path


def test_the_toml_defaults_alone_are_enough_to_start():
    settings = Settings()
    cfg = settings.agent_config()

    assert cfg.query_timeout_s > 0 and cfg.chart_row_cap > 0
    assert settings.max_failed_attempts >= 1 and settings.max_llm_steps >= 1
    with pytest.raises(RuntimeError, match="OPENROUTER_API_KEY"):
        settings.require_api_key()


def test_values_from_the_env_file_override_the_toml_defaults(tmp_path):
    env_file = write_env(
        tmp_path / ".env",
        OPENROUTER_API_KEY="sk-from-the-file",
        OPENROUTER_MODEL="z-ai/glm-5.3",
        TELEMETRY_DB=tmp_path / "t.db",
        RESULTS_DB=tmp_path / "r.db",
        APP_DB=tmp_path / "a.db",
        SEMANTIC_LAYER=tmp_path / "s.yaml",
        RESULT_TTL_HOURS="1",
        QUERY_TIMEOUT_S="0.3",
        CHART_ROW_CAP="5",
        MAX_FAILED_ATTEMPTS="5",
        MAX_LLM_STEPS="4",
        HOST="0.0.0.0",
        PORT="9000",
    )
    settings = Settings(_env_file=env_file)
    cfg = settings.agent_config()

    assert settings.require_api_key() == "sk-from-the-file"
    assert cfg.model == "z-ai/glm-5.3"
    assert cfg.telemetry_db == tmp_path / "t.db"
    assert cfg.results_db == tmp_path / "r.db"
    assert cfg.app_db == tmp_path / "a.db"
    assert cfg.semantic_layer == tmp_path / "s.yaml"
    assert cfg.result_ttl_ms == 3600 * 1000
    assert cfg.query_timeout_s == 0.3
    assert cfg.chart_row_cap == 5
    assert cfg.max_failed_attempts == 5
    assert (settings.max_llm_steps, settings.host, settings.port) == (4, "0.0.0.0", 9000)


def test_the_real_environment_wins_over_the_env_file(tmp_path, monkeypatch):
    env_file = write_env(tmp_path / ".env", MAX_LLM_STEPS="2", QUERY_TIMEOUT_S="5")
    monkeypatch.setenv("MAX_LLM_STEPS", "7")
    settings = Settings(_env_file=env_file)

    assert settings.max_llm_steps == 7  # from the environment
    assert settings.query_timeout_s == 5  # from the file


def test_blank_values_are_treated_as_unset(tmp_path, monkeypatch):
    baseline = Settings()
    env_file = tmp_path / ".env"
    env_file.write_text("QUERY_TIMEOUT_S=\nCHART_ROW_CAP=  \nTELEMETRY_DB=\nOPENROUTER_MODEL=\nMAX_LLM_STEPS=\n")
    monkeypatch.setenv("MAX_FAILED_ATTEMPTS", "")
    settings = Settings(_env_file=env_file)

    assert settings == baseline


def test_dollar_signs_in_env_file_values_are_kept_literally(tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text("OPENROUTER_API_KEY=sk-ab$CD${EF}gh\n")
    settings = Settings(_env_file=env_file)

    assert settings.require_api_key() == "sk-ab$CD${EF}gh"


@pytest.mark.parametrize(
    "variable, bad_value",
    [
        ("MAX_FAILED_ATTEMPTS", "0"),
        ("MAX_LLM_STEPS", "0"),
        ("QUERY_TIMEOUT_S", "0"),
        ("QUERY_TIMEOUT_S", "abc"),
        ("CHART_ROW_CAP", "-1"),
        ("RESULT_TTL_HOURS", "0"),
        ("PORT", "70000"),
    ],
)
def test_invalid_values_are_refused_and_the_error_names_the_setting(tmp_path, variable, bad_value):
    env_file = write_env(tmp_path / ".env", **{variable: bad_value})

    with pytest.raises(ValidationError, match=variable.lower()):
        Settings(_env_file=env_file)


def test_defaults_toml_has_exactly_the_settings_that_have_defaults():
    in_toml = set(tomllib.loads(DEFAULTS_FILE.read_text()))
    fields = set(Settings.model_fields)

    assert in_toml == fields - {"openrouter_api_key"}  # the only setting without a default is the secret


def test_every_setting_is_described():
    undocumented = [name for name, field in Settings.model_fields.items() if not field.description]

    assert undocumented == []


def test_env_example_is_the_generated_one():
    assert ENV_EXAMPLE.read_text() == render_env_example(), (
        "stale .env.example; regenerate with: uv run python -m flood_agent.config > .env.example"
    )


def test_env_example_lists_every_setting_and_copied_verbatim_changes_nothing(tmp_path):
    text = render_env_example()
    assert all(name.upper() in text for name in Settings.model_fields)

    copied = tmp_path / ".env"
    copied.write_text(text)
    assert Settings(_env_file=copied) == Settings()


# --- settings from a .env file change what the Agent does, through the real MCP server -------------


@pytest.fixture
async def agent_from_env_file(tmp_path, telemetry_db, semantic_layer_path):
    agents = []

    async def _build(llm, **env):
        env_file = write_env(
            tmp_path / ".env",
            TELEMETRY_DB=telemetry_db,
            RESULTS_DB=tmp_path / "results.db",
            APP_DB=tmp_path / "app.db",
            SEMANTIC_LAYER=semantic_layer_path,
            **env,
        )
        agent = Agent(Settings(_env_file=env_file).agent_config(), llm)
        await agent.start()
        agents.append(agent)
        return agent

    yield _build
    for agent in agents:
        await agent.aclose()


def text_of(events):
    return "".join(e.text for e in events if isinstance(e, TextDelta))


async def test_max_failed_attempts_from_the_env_file_sets_the_retry_limit(agent_from_env_file):
    def script():
        return ScriptedLLM(
            [tool_call("run_query", sql="SELECT nope FROM flows", title="t")], [TextChunk("recovered")]
        )

    gave_up = await collect(await agent_from_env_file(script(), MAX_FAILED_ATTEMPTS="1"), "q")
    assert "couldn't build" in text_of(gave_up).lower()

    retried = await collect(await agent_from_env_file(script()), "q")
    assert text_of(retried) == "recovered"


async def test_max_llm_steps_from_the_env_file_limits_llm_calls_per_turn(agent_from_env_file):
    llm = ScriptedLLM([tool_call("run_query", sql="SELECT 1 AS a", title="t")])
    events = await collect(await agent_from_env_file(llm, MAX_LLM_STEPS="1"), "q")

    assert STEP_LIMIT_MESSAGE in text_of(events)
    assert len(llm.calls) == 1


def test_the_eval_runner_reads_the_api_key_from_the_env_file(tmp_path, monkeypatch, capsys):
    from flood_agent.evalset.runner import main

    monkeypatch.setitem(Settings.model_config, "env_file", ".env")  # conftest disables the cwd .env
    monkeypatch.chdir(tmp_path)

    assert main(["--questions", "no-such-question-id"]) == 2
    assert "OPENROUTER_API_KEY is not set" in capsys.readouterr().err

    (tmp_path / ".env").write_text("OPENROUTER_API_KEY=sk-from-the-file\n")
    assert main(["--questions", "no-such-question-id"]) == 2
    err = capsys.readouterr().err
    assert "OPENROUTER_API_KEY is not set" not in err
    assert "no matching question ids" in err


def test_tests_are_isolated_from_a_developers_dotenv_and_exports(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env").write_text("MAX_LLM_STEPS=99\n")
    default = tomllib.loads(DEFAULTS_FILE.read_text())["max_llm_steps"]

    assert Settings().max_llm_steps == default


def test_build_app_creates_the_directories_of_both_databases(tmp_path):
    from flood_agent.__main__ import build_app

    env_file = write_env(
        tmp_path / ".env",
        OPENROUTER_API_KEY="sk-x",
        RESULTS_DB=tmp_path / "results-dir" / "results.db",
        APP_DB=tmp_path / "state-dir" / "app.db",
    )
    build_app(Settings(_env_file=env_file))

    assert (tmp_path / "results-dir").is_dir() and (tmp_path / "state-dir").is_dir()


def test_every_path_setting_is_made_absolute(tmp_path):
    env_file = write_env(
        tmp_path / ".env", TELEMETRY_DB="t.db", RESULTS_DB="r.db", APP_DB="a.db", SEMANTIC_LAYER="rel/layer.yaml"
    )
    cfg = Settings(_env_file=env_file).agent_config()

    assert all(p.is_absolute() for p in (cfg.telemetry_db, cfg.results_db, cfg.app_db, cfg.semantic_layer))
