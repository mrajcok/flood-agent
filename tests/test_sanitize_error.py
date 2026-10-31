"""Unit tests for the SQLite error sanitiser (no identifier from an authorizer denial may leak)."""

import pytest

from flood_defender.queries import sanitize_error


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
