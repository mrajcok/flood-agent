"""Developer conveniences, offered only when the app runs under `flood-agent-dev`."""

import sqlite3
from pathlib import Path

from flood_agent import table_names
from flood_agent.semantic_layer import SemanticLayer


def _time_columns(layer: SemanticLayer, table: str) -> list[str]:
    """The Time Key plus every column the Semantic Layer gives as epoch ms, for `table`."""
    cols = (layer.data.get("tables") or {}).get(table, {}).get("columns") or {}
    named = [c for c, m in cols.items() if str((m or {}).get("unit", "")).lower().startswith("epoch ms")]
    key = layer.data.get("time_key")
    return named + ([key] if key in cols and key not in named else [])


def shift_to_now(telemetry_db: Path, layer: SemanticLayer, now_ms: int) -> int:
    """Move every time column of every Telemetry Database table by one amount, so that the latest
    Time Key value becomes `now_ms`. Returns that amount in ms (0 for an empty table, or when the
    data already ends at `now_ms`). The generated data is a fixed week; this keeps "the last hour"
    meaning something without regenerating it."""
    key = layer.data.get("time_key")
    moved = 0
    con = sqlite3.connect(telemetry_db, timeout=30)
    try:
        for table in layer.table_names():
            columns = _time_columns(layer, table)
            if key not in columns:
                continue
            (latest,) = con.execute(f"SELECT MAX({table_names.quote(key)}) FROM {table_names.quote(table)}").fetchone()
            if latest is None:
                continue
            delta = now_ms - latest
            if delta:
                sets = ", ".join(f"{table_names.quote(c)} = {table_names.quote(c)} + ?" for c in columns)
                con.execute(f"UPDATE {table_names.quote(table)} SET {sets}", [delta] * len(columns))
            moved = delta
        con.commit()
    finally:
        con.close()
    return moved
