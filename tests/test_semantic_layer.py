from pathlib import Path

import pytest

from flood_agent.semantic_layer import SemanticLayer

REAL = Path(__file__).parent.parent / "semantic_layer.yaml"


def test_shipped_semantic_layer_declares_dialect_time_key_and_column_examples():
    sl = SemanticLayer(REAL)
    assert sl.data["dialect"] == "sqlite" and sl.data["time_key"] == "etime"
    for name, col in sl.data["tables"]["flows"]["columns"].items():
        assert "example" in col, name
    assert "window_end" not in sl.data and "window_start" not in sl.data


def test_the_system_prompt_asks_for_thousands_separators_in_stated_counts():
    prompt = SemanticLayer(REAL).system_prompt()
    assert "thousands separators" in prompt and "17,392" in prompt



def test_only_the_diameter_subscriber_and_device_columns_are_restricted():
    assert SemanticLayer(REAL).restricted_columns() == {
        "flows": (), "dns_metadata": (), "diameter": ("imsi", "msisdn", "imei")}


def test_a_restricted_that_is_not_a_boolean_is_refused(tmp_path):
    layer = tmp_path / "layer.yaml"
    layer.write_text("tables:\n  subscribers:\n    columns:\n      imsi: {type: TEXT, restricted: 'yes'}\n")
    with pytest.raises(ValueError, match="restricted on column subscribers.imsi"):
        SemanticLayer(layer)


def test_the_system_prompt_says_what_a_restricted_column_means_and_which_columns_are():
    prompt = " ".join(SemanticLayer(REAL).system_prompt().split())
    assert ("Columns marked restricted identify a Subscriber or device. The result of any query that reads a table with "
            "such a column, even without selecting it, and anything queried from that result, can only be downloaded "
            "encrypted.") in prompt
    assert "restricted: true" in prompt
