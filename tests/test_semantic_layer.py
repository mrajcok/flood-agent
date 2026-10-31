from pathlib import Path

from flood_defender.semantic_layer import SemanticLayer

REAL = Path(__file__).parent.parent / "semantic_layer.yaml"


def test_shipped_semantic_layer_declares_dialect_time_key_and_column_examples():
    sl = SemanticLayer(REAL)
    assert sl.data["dialect"] == "sqlite" and sl.data["time_key"] == "etime"
    for name, col in sl.data["tables"]["flows"]["columns"].items():
        assert "example" in col, name
    assert str(sl.window_end) in sl.system_prompt()
