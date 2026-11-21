"""Developer conveniences, offered only when the app runs under `flood-agent-dev`."""

import sqlite3
from pathlib import Path

from flood_agent import table_names
from flood_agent.semantic_layer import SemanticLayer


def _time_columns(layer: SemanticLayer, table: str) -> list[str]:
    """The table's Time Key plus every column the Semantic Layer gives as epoch ms."""
    cols = (layer.data.get("tables") or {}).get(table, {}).get("columns") or {}
    named = [c for c, m in cols.items() if str((m or {}).get("unit", "")).lower().startswith("epoch ms")]
    key = layer.time_key(table)
    return named + ([key] if key in cols and key not in named else [])


def shift_to_now(telemetry_db: Path, layer: SemanticLayer, now_ms: int) -> int:
    """Move every time column of every Telemetry Database table by one amount, so that the latest
    Time Key value across all tables becomes `now_ms`. Returns that amount in ms (0 for empty tables,
    or when the data already ends at `now_ms`). One delta keeps tables aligned, so joins still match.
    The generated data is a fixed week; this keeps "the last hour" meaningful without regenerating it."""
    con = sqlite3.connect(telemetry_db, timeout=30)
    try:
        latest = None
        for table in layer.table_names():
            key = layer.time_key(table)
            if not key:
                continue
            (top,) = con.execute(f"SELECT MAX({table_names.quote(key)}) FROM {table_names.quote(table)}").fetchone()
            if top is not None and (latest is None or top > latest):
                latest = top
        if latest is None:
            return 0
        delta = now_ms - latest
        if delta:
            for table in layer.table_names():
                columns = _time_columns(layer, table)
                if not columns:
                    continue
                sets = ", ".join(f"{table_names.quote(c)} = {table_names.quote(c)} + ?" for c in columns)
                con.execute(f"UPDATE {table_names.quote(table)} SET {sets}", [delta] * len(columns))
        con.commit()
        return delta
    finally:
        con.close()
