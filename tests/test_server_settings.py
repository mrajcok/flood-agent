"""The MCP server's static settings survive the trip through its launch environment."""

from pathlib import Path

from flood_agent.lineage import TableSchema
from flood_agent.mcp_server import ServerSettings


def test_settings_round_trip_through_the_launch_environment():
    settings = ServerSettings(
        telemetry_db=Path("/data/telemetry.db"),
        users_dir=Path("/data/users"),
        users=("alice", "bob"),
        query_timeout_s=2.5,
        chart_row_cap=99,
        telemetry_tables={"flows": TableSchema(("sip", "dip")),
                          "subscribers": TableSchema(("imsi", "apn"), frozenset({"imsi"}))},
    )
    env = settings.to_env()
    assert set(env) == {"TELEMETRY_DB", "USERS_DIR", "USER_NAMES", "QUERY_TIMEOUT_S", "CHART_ROW_CAP", "TELEMETRY_TABLES"}
    assert ServerSettings.from_env(env) == settings
