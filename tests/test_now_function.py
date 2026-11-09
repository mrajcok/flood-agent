"""now(): the current time in epoch ms, available to the LLM's SQL so it never writes absolute timestamps."""

import time

from flood_agent import queries
from flood_agent.mcp_server import ServerSettings
from flood_agent.semantic_layer import SemanticLayer


def count(telemetry_db, tmp_path, sql):
    return queries.run_count_query(telemetry_db, tmp_path / "results.db", sql, 5.0)


def test_now_is_the_wall_clock_in_epoch_ms(telemetry_db, tmp_path):
    before = int(time.time() * 1000)
    # flows hold 2023 timestamps, so "the last hour" is empty and "before now" is all of them
    assert count(telemetry_db, tmp_path, "SELECT COUNT(*) FROM flows WHERE etime > now() - 3600000") == {
        "success": True, "count": 0,
    }
    assert count(telemetry_db, tmp_path, "SELECT COUNT(*) FROM flows WHERE etime < now()")["count"] == 3
    assert count(telemetry_db, tmp_path, f"SELECT COUNT(*) FROM (SELECT now() AS n) WHERE n >= {before}")["count"] == 1


def test_a_fixed_now_can_be_set_for_reproducible_runs(telemetry_db, tmp_path, monkeypatch):
    monkeypatch.setenv("FIXED_NOW_MS", "1700000003000")  # just after the test flows' etimes
    sql = "SELECT COUNT(*) FROM flows WHERE etime > now() - 500"
    assert count(telemetry_db, tmp_path, sql)["count"] == 1


def test_the_server_passes_a_fixed_now_to_its_process():
    s = ServerSettings(telemetry_db="t", results_db="r", app_db=None, query_timeout_s=1, chart_row_cap=1, fixed_now_ms=42)  # pyright: ignore
    assert s.to_env()["FIXED_NOW_MS"] == "42"
    assert ServerSettings.from_env(s.to_env()).fixed_now_ms == 42
    assert "FIXED_NOW_MS" not in ServerSettings(telemetry_db="t", results_db="r", app_db=None, query_timeout_s=1, chart_row_cap=1).to_env()  # pyright: ignore


def test_the_system_prompt_has_no_time_in_it_so_it_can_be_cached():
    from pathlib import Path

    layer = SemanticLayer(Path(__file__).parent.parent / "semantic_layer.yaml")
    prompt = layer.system_prompt()
    assert prompt == layer.system_prompt()
    assert "window_end" not in prompt and "epoch ms 1" not in prompt
    assert "now()" in prompt
