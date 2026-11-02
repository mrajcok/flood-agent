from pathlib import Path

from flood_agent.semantic_layer import SemanticLayer

REAL = Path(__file__).parent.parent / "semantic_layer.yaml"


def test_shipped_semantic_layer_declares_dialect_time_key_and_column_examples():
    sl = SemanticLayer(REAL)
    assert sl.data["dialect"] == "sqlite" and sl.data["time_key"] == "etime"
    for name, col in sl.data["tables"]["flows"]["columns"].items():
        assert "example" in col, name
    assert str(sl.window_end) in sl.system_prompt()


def test_the_system_prompt_asks_for_thousands_separators_in_stated_counts():
    prompt = SemanticLayer(REAL).system_prompt()
    assert "thousands separators" in prompt and "17,392" in prompt
