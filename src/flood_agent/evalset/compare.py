"""Result Table comparison rules for the eval runner.

A model's table is judged against the reference table like this:

* Column names are ignored (models alias freely) and so is column order. Each reference
  column must be matched to a distinct model column holding the same values.
* Extra model columns are allowed (the question is answered if every requested value is
  present); their count is reported as `extra_columns`.
* Rows are compared as a multiset, unless the question is `ordered`, in which case
  row order must match too.
* Numbers: ints and floats are interchangeable (100 == 100.0) and compare with a relative
  tolerance (default 1e-6, per-question override) plus a tiny absolute floor. Strings are
  exact and case-sensitive; a string never equals a number. NULL only equals NULL.
* Columns must line up row-wise: the matched columns together must reproduce the
  reference rows, not merely each column's values separately.
"""

import math
from dataclasses import dataclass

DEFAULT_TOLERANCE = 1e-6
ABS_FLOOR = 1e-9


@dataclass
class Comparison:
    ok: bool
    reason: str = ""
    extra_columns: int = 0


def _rank(v) -> int:
    if v is None:
        return 0
    if isinstance(v, (int, float)):
        return 1
    return 2


def _sort_key(v):
    r = _rank(v)
    if r == 1:
        return (r, float(f"{float(v):.6g}"))
    return (r, "" if v is None else str(v))


def _eq(a, b, tol: float) -> bool:
    ra, rb = _rank(a), _rank(b)
    if ra != rb:
        return False
    if ra == 0:
        return True
    if ra == 1:
        return math.isclose(float(a), float(b), rel_tol=tol, abs_tol=ABS_FLOOR)
    return a == b


def _rows_eq(a: tuple, b: tuple, tol: float) -> bool:
    return all(_eq(x, y, tol) for x, y in zip(a, b))


def _row_key(row):
    return tuple(_sort_key(v) for v in row)


def _seq_eq(a: list, b: list, tol: float, ordered: bool) -> bool:
    if len(a) != len(b):
        return False
    if not ordered:
        a, b = sorted(a, key=_sort_key), sorted(b, key=_sort_key)
    return all(_eq(x, y, tol) for x, y in zip(a, b))


def _tables_eq(a: list[tuple], b: list[tuple], tol: float, ordered: bool) -> bool:
    if not ordered:
        a, b = sorted(a, key=_row_key), sorted(b, key=_row_key)
    return all(_rows_eq(x, y, tol) for x, y in zip(a, b))


def compare(
    ref_columns: list[str],
    ref_rows: list[tuple],
    got_columns: list[str],
    got_rows: list[tuple],
    ordered: bool = False,
    tolerance: float = DEFAULT_TOLERANCE,
) -> Comparison:
    k = len(ref_columns)
    if len(got_columns) < k:
        return Comparison(False, f"column count: need {k} column(s), got {len(got_columns)}")
    if len(got_rows) != len(ref_rows):
        return Comparison(False, f"row count: expected {len(ref_rows)}, got {len(got_rows)}")
    extra = len(got_columns) - k
    if not ref_rows:
        return Comparison(True, extra_columns=extra)

    ref_cols = [[r[i] for r in ref_rows] for i in range(k)]
    got_cols = [[r[j] for r in got_rows] for j in range(len(got_columns))]
    candidates = [
        [j for j, g in enumerate(got_cols) if _seq_eq(rc, g, tolerance, ordered)] for rc in ref_cols
    ]
    if any(not c for c in candidates):
        # Distinguish "right values, wrong order" from "wrong values".
        if ordered and all(
            any(_seq_eq(rc, g, tolerance, False) for g in got_cols) for rc in ref_cols
        ):
            return Comparison(False, "order: values match but row order differs")
        return Comparison(False, "values: a requested column has no matching column in the result")

    def search(i: int, used: tuple) -> bool:
        if i == k:
            projected = [tuple(r[j] for j in used) for r in got_rows]
            return _tables_eq(ref_rows, projected, tolerance, ordered)
        return any(search(i + 1, used + (j,)) for j in candidates[i] if j not in used)

    if search(0, ()):
        return Comparison(True, extra_columns=extra)
    return Comparison(False, "values: columns match individually but rows do not line up")
