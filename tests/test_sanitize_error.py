"""Unit tests for the SQLite error sanitiser (no identifier from an authorizer denial may leak)."""

import pytest

from flood_agent.queries import sanitize_error


@pytest.mark.parametrize("name", ["rt_1a1f9db6", "rt_abcdefab", "rt_12345678", "rt_00ff00ff", "rt_a1b2c3d4"])
@pytest.mark.parametrize("col", ["dport", "total_bytes", "col1"])
def test_authorizer_denial_is_redacted_wholesale(name, col):
    for msg in (f"access to results.{name}.{col} is prohibited", f"access to results.{name} is prohibited",
                "not authorized"):
        out = sanitize_error(msg)
        assert name not in out and col not in out and "rt_" not in out
        assert out == "Access to a table or column was denied."


def test_qualified_references_are_redacted():
    out = sanitize_error("no such table: results.rt_1a1f9db6")
    assert "rt_1a1f9db6" not in out and "1a1f9db6" not in out
    assert "main.flows" not in sanitize_error("no such column: main.flows.zzz")


def test_ordinary_errors_still_informative_and_value_free():
    out = sanitize_error('no such column: nope near "x": syntax error at 42')
    assert "no such column: nope" in out and "42" not in out and '"x"' not in out


@pytest.mark.parametrize("name", ["rt_20200101_000000", "rt_20200101_000000_2"])
def test_a_generated_table_name_is_kept_and_the_numbers_around_it_are_not(name):
    out = sanitize_error(f"no such table: {name} at 42, not {name}.7", keep_table_names=True)
    assert out == f"no such table: {name} at <n>, not {name}.<n>"


@pytest.mark.parametrize(
    "token",
    ["rt_12345678", "rt_20200101_0000007", "rt_202001011_000000", "xrt_20200101_000000", "_rt_20200101_000000",
     "rt_20200101_000000x", "rt_20200101_000000_2x", "rt_20200101_000000_x", "4rt_20200101_000000"],
)
def test_only_a_whole_generated_table_name_is_kept(token):
    assert token not in sanitize_error(f"no such column: {token}", keep_table_names=True)


def test_a_generated_table_name_inside_a_literal_or_a_qualified_reference_is_redacted():
    quoted, qualified = "near 'rt_20200101_000000': syntax error", "no such table: results.rt_20200101_000000"
    assert sanitize_error(quoted, keep_table_names=True) == "near <literal>: syntax error"
    assert sanitize_error(qualified, keep_table_names=True) == "no such table: <table>"


def test_a_generated_table_name_is_not_kept_unless_asked_for():
    assert "rt_20200101_000000" not in sanitize_error("no such table: rt_20200101_000000")
