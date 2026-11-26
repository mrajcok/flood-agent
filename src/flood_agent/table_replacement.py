"""Table replacement: put a query's rows under a live table's name, all or nothing.

A Refresh (run the table's stored query again) and a SQL Cell re-run (run the Cell's current SQL) both end the
same way: the table keeps its name, gets new rows, and its Table Catalog row says what those rows are. This
module owns those steps so the two callers cannot drift apart, and so a database that replaces a table in one
statement only has one place to change.
"""

import dataclasses
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Iterator

from flood_agent import lineage
from flood_agent.catalog import Catalog, CellRun, Table, canonical
from flood_agent.queries import QueryRunner


class RefreshInProgress(Exception):
    """The table is already being replaced (refreshed, or re-run by its SQL Cell)."""


class ReplacementFailed(Exception):
    """The query did not give rows (it was rejected, failed, timed out or was stopped); the table is as it was.
    `error` is the sanitised message, `raw_error` the full one (for the log and the user), `stopped` whether
    the stop flag ended it."""

    def __init__(self, error: str, raw_error: str, stopped: bool = False):
        super().__init__(error)
        self.error, self.raw_error, self.stopped = error, raw_error, stopped


@dataclass(frozen=True)
class Rerun:
    """What a SQL Cell re-run changes besides the rows: the table now belongs to the Cell's new Run and carries
    the new title. Its SQL and row cap are the ones passed to `replace`."""

    owner: CellRun
    title: str


@dataclass(frozen=True)
class Replaced:
    """A finished replacement: the table as the Table Catalog now describes it, and how many rows it holds."""

    table: Table
    row_count: int


class TableReplacement:
    def __init__(self, tables: Catalog, runner: QueryRunner):
        self.tables, self.runner = tables, runner
        self._busy: set[str] = set()  # tables being replaced now
        self._lock = threading.Lock()
        self._swapping = threading.Lock()  # held while a table's catalog row and rows do not yet match

    @contextmanager
    def settled(self) -> Iterator[None]:
        """While held, every table's catalog row describes the rows under its name: no replacement is between
        writing the one and swapping in the other. For a reader that must have both from the same side."""
        with self._swapping:
            yield

    def replace(self, name: str, sql: str, max_rows: int, *, stop: threading.Event | None = None,
                rerun: Rerun | None = None) -> Replaced:
        """Run `sql` and put its rows (at most `max_rows`) under live table `name`; return the updated table and
        its row count.

        The rows are built under a temporary name first, then the Table Catalog is written in one step (the
        refresh fields, and for a `rerun` the new owner, title, SQL and row cap), and the swap comes last, so
        a failure leaves the rows and the catalog as they were. Nothing after the swap can undo the catalog.

        KeyError if unknown, `TableExpired` if no longer live, `RefreshInProgress` if it is being replaced,
        `ReplacementFailed` if the query fails or is stopped."""
        before = self.tables[name]
        with self._one_at_a_time(before.name):
            result = self.runner.run(sql, max_rows, rerun.title if rerun else before.title, stop)
            if not result["success"]:
                raise ReplacementFailed(result["error"], result.get("raw_error") or result["error"],
                                        bool(result.get("stopped")))
            temp, undo = result["result_table"], None
            restricted = lineage.restricted_columns(result["lineage"])
            after = dataclasses.replace(before, columns=result["columns"], truncated_to=result.get("truncated_to"),
                                        restricted_columns=tuple(restricted or ()),
                                        lineage_unknown=restricted is None)
            if rerun:
                after = dataclasses.replace(after, cell_id=rerun.owner.cell_id, run_id=rerun.owner.run_id,
                                            title=rerun.title, sql=sql, max_rows=max_rows)
            with self._swapping:
                try:
                    # The one catalog write is the step that can be undone, and it refuses a table a sweep expired.
                    undo = self.tables.record_replacement(after)
                    self.runner.swap(temp, before.name)
                except BaseException:
                    try:
                        if undo is not None:
                            self.tables.restore_replacement(undo)
                    finally:
                        self.runner.drop(temp)  # even if the restore fails
                    self.tables[before.name]  # a sweep dropped the table meanwhile: TableExpired, the plain answer
                    raise
            return Replaced(self.tables[before.name], result["row_count"])

    @contextmanager
    def _one_at_a_time(self, name: str) -> Iterator[None]:
        key = canonical(name)
        with self._lock:
            if key in self._busy:
                raise RefreshInProgress(f"{name} is already being replaced; try again when that finishes.")
            self._busy.add(key)
        try:
            yield
        finally:
            with self._lock:
                self._busy.discard(key)
