"""The Semantic Layer is ready for more than one Telemetry table."""

from pathlib import Path

import pytest

from flood_agent.semantic_layer import SemanticLayer

REAL = Path(__file__).parent.parent / "semantic_layer.yaml"

TWO_TABLES = """\
dialect: sqlite
time_key: etime
tables:
  flows:
    columns:
      proto: {type: INTEGER, enum: {6: TCP}}
      etime: {type: INTEGER, unit: epoch ms UTC}
      bytes: {type: INTEGER, unit: bytes}
  other:
    columns:
      proto: {type: INTEGER, enum: {6: TCP}}
      etime: {type: INTEGER, unit: epoch ms UTC}
      kind: {type: INTEGER, enum_bits: {1: A, 2: B}}
"""


def layer_from(tmp_path, text):
    p = tmp_path / "layer.yaml"
    p.write_text(text)
    return SemanticLayer(p)


def test_the_system_prompt_is_identical_across_calls():
    layer = SemanticLayer(REAL)
    assert layer.system_prompt() == layer.system_prompt()


def test_a_table_missing_its_time_key_column_fails_at_load(tmp_path):
    text = TWO_TABLES.replace("      etime: {type: INTEGER, unit: epoch ms UTC}\n      kind", "      kind")
    with pytest.raises(ValueError, match=r"other.*etime"):
        layer_from(tmp_path, text)


def test_a_table_may_set_its_own_time_key(tmp_path):
    text = TWO_TABLES.replace("  other:\n", "  other:\n    time_key: seen\n").replace(
        "      kind:", "      seen: {type: INTEGER}\n      kind:"
    )
    layer_from(tmp_path, text)  # loads: 'other' has its own Time Key


def test_conflicting_same_named_columns_fail_at_load(tmp_path):
    text = TWO_TABLES.replace(
        "      proto: {type: INTEGER, enum: {6: TCP}}\n      etime: {type: INTEGER, unit: epoch ms UTC}\n      kind",
        "      proto: {type: INTEGER, enum: {6: TCP, 17: UDP}}\n      etime: {type: INTEGER, unit: epoch ms UTC}\n      kind",
    )
    with pytest.raises(ValueError, match="proto"):
        layer_from(tmp_path, text)


def test_column_formats_look_a_column_up_across_all_tables(tmp_path):
    f = layer_from(tmp_path, TWO_TABLES).column_formats(["proto", "kind", "bytes", "etime", "nope"])
    assert f["proto"] == {"kind": "enum", "map": {"6": "TCP"}}
    assert f["kind"] == {"kind": "flags", "map": {"1": "A", "2": "B"}}
    assert f["bytes"] == {"kind": "bytes"} and f["etime"] == {"kind": "time"}
    assert f["nope"] == {"kind": "plain"}
