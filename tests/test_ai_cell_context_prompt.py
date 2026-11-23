"""The AI Cell context module's system prompt: named sections, rendered in ADR 0007 order and parsed back."""

import json
from pathlib import Path

from flood_agent import table_names
from flood_agent.ai_cell_context import SystemPrompt, parse_system_prompt
from flood_agent.catalog import Table
from flood_agent.semantic_layer import SYSTEM_INSTRUCTIONS, SemanticLayer

# Regression snapshot of the code before the sections existed; regenerate for an intentional change to the prompt.
GOLDEN = json.loads((Path(__file__).parent / "golden" / "system_prompts.json").read_text())

SMALL = """\
dialect: sqlite
time_key: etime
tables:
  flows:
    columns:
      proto: {type: INTEGER, enum: {6: TCP}}
      etime: {type: INTEGER, unit: epoch ms UTC}
"""


def _table(name, title, request):
    return Table(name=name, kind="result", title=title, columns=[{"name": "a", "type": "INTEGER"}, {"name": "b", "type": "TEXT"}],
                 request=request, truncated_to=None, retention_ms=0, expires_in_ms=0)


AI = [_table("rt_1", "Top talkers", "show top talkers")]
SQL = AI + [_table("rt_2", "Port 53", table_names.SQL_CELL_REQUEST)]


def _layer(tmp_path):
    p = tmp_path / "layer.yaml"
    p.write_text(SMALL)
    return SemanticLayer(p)


def _prompt(layer, tables):
    return SystemPrompt.build(SYSTEM_INSTRUCTIONS, layer.render(), tables)


def test_the_rendered_prompt_is_byte_identical_to_before_with_no_tables_ai_cell_tables_and_sql_cell_tables(tmp_path):
    layer = _layer(tmp_path)
    assert _prompt(layer, []).render() == GOLDEN["none"]
    assert _prompt(layer, AI).render() == GOLDEN["ai"]
    assert _prompt(layer, SQL).render() == GOLDEN["sql"]


def test_the_semantic_layer_builds_its_prompt_through_the_module(tmp_path):
    layer = _layer(tmp_path)
    assert layer.system_prompt() == GOLDEN["none"]
    assert layer.system_prompt(SQL) == GOLDEN["sql"]


def test_parsing_a_rendered_prompt_gives_back_its_sections(tmp_path):
    layer = _layer(tmp_path)
    for key, tables in (("none", []), ("ai", AI), ("sql", SQL)):
        parsed = parse_system_prompt(GOLDEN[key])
        assert parsed == _prompt(layer, tables)
        assert parsed.render() == GOLDEN[key]
    assert parsed.instructions == SYSTEM_INSTRUCTIONS + "\n\n"
    assert parsed.semantic_layer == "# Semantic Layer\n" + layer.render()
    assert parsed.result_tables.startswith("# Result Tables\n")


def test_a_prompt_without_the_expected_headings_lands_wholly_in_the_instructions():
    for text in ("plain", "", "rules\n# Result Tables\nx"):
        parsed = parse_system_prompt(text)
        assert parsed == SystemPrompt(text, "", "")
        assert parsed.render() == text
    assert parse_system_prompt(None) == SystemPrompt("", "", "")


def test_the_parts_are_instructions_then_the_layer_with_its_heading_then_the_rest():
    prompt = "rules\n# Semantic Layer\ntables: x\n\n# Result Tables\nNone.\n"
    parsed = parse_system_prompt(prompt)
    assert parsed.parts() == ("rules\n", "# Semantic Layer\ntables: x\n", "# Result Tables\nNone.\n")


def test_heading_like_text_mid_line_does_not_split_the_prompt():
    prompt = "see # Semantic Layer\nrules\n\n# Semantic Layer\nnote: a # Result Tables\nb\n\n# Result Tables\nNone.\n"
    parsed = parse_system_prompt(prompt)
    assert parsed.instructions == "see # Semantic Layer\nrules\n\n"
    assert parsed.semantic_layer == "# Semantic Layer\nnote: a # Result Tables\nb\n"
    assert parsed.result_tables == "# Result Tables\nNone.\n"
    assert parsed.render() == prompt
