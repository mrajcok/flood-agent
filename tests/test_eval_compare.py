"""Comparison rules: how a model's Result Table is judged against the reference."""

from flood_agent.evalset.compare import compare

REF_COLS = ["sip", "total_bytes"]
REF = [("a", 100), ("b", 50), ("c", 7)]


def test_exact_match_passes():
    assert compare(REF_COLS, REF, ["sip", "total_bytes"], REF).ok


def test_row_order_ignored_unless_ordered():
    shuffled = [("c", 7), ("a", 100), ("b", 50)]
    assert compare(REF_COLS, REF, REF_COLS, shuffled).ok
    r = compare(REF_COLS, REF, REF_COLS, shuffled, ordered=True)
    assert not r.ok and "order" in r.reason


def test_ordered_match_passes():
    assert compare(REF_COLS, REF, ["x", "y"], REF, ordered=True).ok


def test_column_names_and_order_do_not_matter():
    swapped = [(b, a) for a, b in REF]
    r = compare(REF_COLS, REF, ["bytes_total", "source"], swapped)
    assert r.ok and r.extra_columns == 0


def test_extra_columns_allowed_and_reported():
    wide = [(a, b, "x") for a, b in REF]
    r = compare(REF_COLS, REF, ["a", "b", "c"], wide)
    assert r.ok and r.extra_columns == 1


def test_missing_column_fails():
    r = compare(REF_COLS, REF, ["only"], [(a,) for a, _ in REF])
    assert not r.ok and "column" in r.reason


def test_wrong_values_fail():
    r = compare(REF_COLS, REF, REF_COLS, [("a", 100), ("b", 51), ("c", 7)])
    assert not r.ok


def test_row_count_mismatch_fails():
    r = compare(REF_COLS, REF, REF_COLS, REF[:2])
    assert not r.ok and "row count" in r.reason


def test_columns_must_line_up_row_wise():
    # each column's values match as a multiset, but the pairing is wrong
    wrong = [("a", 50), ("b", 100), ("c", 7)]
    assert not compare(REF_COLS, REF, REF_COLS, wrong).ok


def test_numeric_tolerance_and_int_float_equivalence():
    assert compare(["v"], [(100,)], ["v"], [(100.0,)]).ok
    assert compare(["v"], [(1.0,)], ["v"], [(1.0000001,)]).ok
    assert not compare(["v"], [(1.0,)], ["v"], [(1.01,)]).ok
    assert compare(["v"], [(1.0,)], ["v"], [(1.01,)], tolerance=0.02).ok


def test_strings_are_exact_and_not_numbers():
    assert not compare(["v"], [("443",)], ["v"], [(443,)]).ok
    assert not compare(["v"], [("TCP",)], ["v"], [("tcp",)]).ok


def test_nulls_match_nulls():
    assert compare(["v"], [(None,), (1,)], ["v"], [(1,), (None,)]).ok
