"""The MCP server's static settings survive the trip through its launch environment."""

from pathlib import Path

from flood_defender.mcp_server import ServerSettings


def test_settings_round_trip_through_the_launch_environment():
    settings = ServerSettings(
        telemetry_db=Path("/data/telemetry.db"),
        results_db=Path("/data/results.db"),
        app_db=Path("/data/app.db"),
        query_timeout_s=2.5,
        max_result_rows=1234,
        chart_row_cap=99,
    )
    env = settings.to_env()
    assert set(env) == {"TELEMETRY_DB", "RESULTS_DB", "APP_DB", "QUERY_TIMEOUT_S", "MAX_RESULT_ROWS", "CHART_ROW_CAP"}
    assert ServerSettings.from_env(env) == settings
