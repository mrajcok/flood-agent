"""Ocient string functions for domain names, with SQLite stand-ins in dev."""

from pathlib import Path

import pytest

from flood_agent.semantic_layer import SemanticLayer

FUNCTIONS = ("ENDSWITH", "STARTSWITH", "SPLIT_PART", "REGEXP_SUBSTR", "REGEXP_COUNT", "REVERSE")


@pytest.fixture
def value(query_runner):
    """`value(expr)`: a scalar SQL expression, evaluated by a query."""
    runner = query_runner()

    def _value(expr):
        made = runner.run(f"SELECT {expr}", max_rows=1)
        assert made["success"], made
        return next(runner.all_rows(made["result_table"])[1])[0]

    return _value


@pytest.mark.parametrize(
    "expr, expected",
    [
        ("ENDSWITH('a.example.com', '.example.com')", 1),
        ("ENDSWITH('a.example.com', '.example.org')", 0),
        ("STARTSWITH('amp.example.net', 'amp.')", 1),
        ("STARTSWITH('amp.example.net', 'x')", 0),
        ("SPLIT_PART('a.b.c', '.', 1)", "a"),
        ("SPLIT_PART('a.b.c', '.', 3)", "c"),
        ("SPLIT_PART('a.b.c', '.', 4)", ""),
        ("SPLIT_PART('a.b.c', '.', 0)", ""),
        ("REGEXP_SUBSTR('abc123def', '[0-9]+')", "123"),
        ("REGEXP_SUBSTR('abc', '[0-9]+')", None),
        ("REGEXP_COUNT('a.b.c', '[.]')", 2),
        ("REGEXP_COUNT('abc', '[0-9]')", 0),
        ("REVERSE('abc')", "cba"),
    ],
)
def test_documented_results(value, expr, expected):
    assert value(expr) == expected


@pytest.mark.parametrize(
    "expr",
    [
        "ENDSWITH(NULL, 'a')", "ENDSWITH('a', NULL)", "STARTSWITH(NULL, 'a')", "STARTSWITH('a', NULL)",
        "SPLIT_PART(NULL, '.', 1)", "SPLIT_PART('a', NULL, 1)", "SPLIT_PART('a', '.', NULL)",
        "REGEXP_SUBSTR(NULL, 'a')", "REGEXP_SUBSTR('a', NULL)",
        "REGEXP_COUNT(NULL, 'a')", "REGEXP_COUNT('a', NULL)", "REVERSE(NULL)",
    ],
)
def test_a_null_argument_returns_null(value, expr):
    assert value(expr) is None


@pytest.mark.parametrize(
    "expr",
    [
        "ENDSWITH(5, 'a')", "ENDSWITH('a', 5)", "STARTSWITH(5, 'a')", "STARTSWITH('a', 5)",
        "SPLIT_PART(5, '.', 1)", "SPLIT_PART('a', 5, 1)", "SPLIT_PART('a.b', '.', 'x')",
        "REGEXP_SUBSTR(5, 'a')", "REGEXP_SUBSTR('a', 5)",
        "REGEXP_COUNT(5, 'a')", "REGEXP_COUNT('a', 5)", "REVERSE(5)", "REVERSE(X'ab')",
    ],
)
def test_a_non_text_argument_returns_null(value, expr):
    assert value(expr) is None


def test_the_functions_work_on_any_text_column(query_runner, value):
    result = query_runner().count("SELECT COUNT(*) FROM flows WHERE STARTSWITH(sip, '1')")
    assert result["success"] and result["count"] >= 1
    assert value("(SELECT REVERSE(sip) FROM flows LIMIT 1)") is not None


def test_the_semantic_layer_lists_them_and_the_prompt_carries_them():
    layer = SemanticLayer(Path(__file__).parent.parent / "semantic_layer.yaml")
    prompt = layer.system_prompt()
    for name in FUNCTIONS:
        assert name in prompt
