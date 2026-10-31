"""Configuration loading from a .env file and the environment."""

from flood_defender.agent import Agent
from flood_defender.config import agent_config_from_env, load_env_file
from flood_defender.llm import DEFAULT_MODEL, TextChunk

from conftest import ScriptedLLM, collect, last_tool_result, tool_call


def write_env(path, **values):
    path.write_text("".join(f"{k}={v}\n" for k, v in values.items()))
    return path


def test_limits_and_paths_come_from_the_env_file(tmp_path):
    env_file = write_env(
        tmp_path / ".env",
        TELEMETRY_DB=tmp_path / "t.db",
        RESULTS_DB=tmp_path / "r.db",
        APP_DB=tmp_path / "a.db",
        SEMANTIC_LAYER=tmp_path / "s.yaml",
        QUERY_TIMEOUT_S="0.3",
        MAX_RESULT_ROWS="2",
        CHART_ROW_CAP="5",
        RESULT_TTL_HOURS="1",
        OPENROUTER_MODEL="z-ai/glm-5.3",
    )
    environ: dict[str, str] = {}
    load_env_file(env_file, environ)
    cfg = agent_config_from_env(environ)

    assert cfg.telemetry_db == tmp_path / "t.db"
    assert cfg.results_db == tmp_path / "r.db"
    assert cfg.app_db == tmp_path / "a.db"
    assert cfg.semantic_layer == tmp_path / "s.yaml"
    assert cfg.query_timeout_s == 0.3
    assert cfg.max_result_rows == 2
    assert cfg.chart_row_cap == 5
    assert cfg.result_ttl_ms == 3600 * 1000
    assert cfg.model == "z-ai/glm-5.3"


def test_the_real_environment_wins_over_the_env_file(tmp_path):
    env_file = write_env(tmp_path / ".env", MAX_RESULT_ROWS="2", QUERY_TIMEOUT_S="5")
    environ = {"MAX_RESULT_ROWS": "7"}
    load_env_file(env_file, environ)
    cfg = agent_config_from_env(environ)

    assert cfg.max_result_rows == 7  # from the environment
    assert cfg.query_timeout_s == 5  # from the file


def test_a_missing_env_file_and_empty_environment_give_the_defaults(tmp_path):
    environ: dict[str, str] = {}
    load_env_file(tmp_path / "no-such.env", environ)
    cfg = agent_config_from_env(environ)

    assert environ == {}
    assert cfg.query_timeout_s == 30.0
    assert cfg.max_result_rows == 1_000_000
    assert cfg.chart_row_cap == 10_000
    assert cfg.result_ttl_ms == 24 * 3600 * 1000
    assert cfg.model == DEFAULT_MODEL


async def test_limits_loaded_from_the_env_file_reach_the_mcp_server(tmp_path, telemetry_db, semantic_layer_path):
    def run_all_flows(env_values):
        env_file = write_env(
            tmp_path / ".env",
            TELEMETRY_DB=telemetry_db,
            RESULTS_DB=tmp_path / "results.db",
            APP_DB=tmp_path / "app.db",
            SEMANTIC_LAYER=semantic_layer_path,
            **env_values,
        )
        environ: dict[str, str] = {}
        load_env_file(env_file, environ)
        llm = ScriptedLLM([tool_call("run_query", sql="SELECT * FROM flows", title="t")], [TextChunk("ok")])
        return llm, Agent(agent_config_from_env(environ), llm)

    llm, agent = run_all_flows({"MAX_RESULT_ROWS": "2"})
    await collect(agent, "q")
    capped = last_tool_result(llm.calls[1])
    assert capped["success"] is False
    assert "aggregate" in capped["error"].lower() or "filter" in capped["error"].lower()

    llm, agent = run_all_flows({})
    await collect(agent, "q")
    assert last_tool_result(llm.calls[1])["success"] is True


def test_blank_values_in_the_env_file_are_treated_as_unset(tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text("QUERY_TIMEOUT_S=\nMAX_RESULT_ROWS=\nCHART_ROW_CAP=  \nTELEMETRY_DB=\nOPENROUTER_MODEL=\n")
    environ: dict[str, str] = {}
    load_env_file(env_file, environ)
    cfg = agent_config_from_env(environ)

    assert environ == {}
    assert cfg.query_timeout_s == 30.0
    assert cfg.max_result_rows == 1_000_000
    assert cfg.chart_row_cap == 10_000
    assert cfg.telemetry_db.name == "telemetry.db"
    assert cfg.model == DEFAULT_MODEL


def test_dollar_signs_in_env_file_values_are_kept_literally(tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text("OPENROUTER_API_KEY=sk-ab$CD${EF}gh\n")
    environ: dict[str, str] = {}
    load_env_file(env_file, environ)

    assert environ["OPENROUTER_API_KEY"] == "sk-ab$CD${EF}gh"


def test_the_eval_runner_reads_the_api_key_from_the_env_file(tmp_path, monkeypatch, capsys):
    from flood_defender.evalset.runner import main

    monkeypatch.setenv("OPENROUTER_API_KEY", "placeholder")
    monkeypatch.delenv("OPENROUTER_API_KEY")  # restored to "unset" when the test ends
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env").write_text("OPENROUTER_API_KEY=sk-from-the-file\n")

    code = main(["--questions", "no-such-question-id"])

    err = capsys.readouterr().err
    assert "OPENROUTER_API_KEY is not set" not in err
    assert "no matching question ids" in err
    assert code == 2
