"""Table Replacement: put a query's rows under a live table's name, all or nothing.

A Refresh (run the table's stored query again) and a SQL Cell re-run (run the Cell's current SQL) both end the
same way: the table keeps its name, gets new rows, and its Table Catalog row says what those rows are. This
module owns those steps so the two callers cannot drift apart, and so a database that replaces a table in one
statement only has one place to change.
"""

import threading
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

from flood_agent import queries
from flood_agent.catalog import Catalog, CellRun, Table, canonical


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
    def __init__(self, tables: Catalog, telemetry_db: Path, results_db: Path, timeout_s: float):
        self.tables = tables
        self.telemetry_db, self.results_db, self.timeout_s = telemetry_db, results_db, timeout_s
        self._busy: set[str] = set()  # tables being replaced now
        self._lock = threading.Lock()

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
            result = queries.run_query(
                self.telemetry_db, self.results_db, sql, self.timeout_s, max_rows, self.tables.readable(),
                rerun.title if rerun else before.title, stop,
            )
            if not result["success"]:
                raise ReplacementFailed(result["error"], result.get("raw_error") or result["error"],
                                        bool(result.get("stopped")))
            temp, recorded = result["result_table"], None
            try:
                # The one catalog write is the step that can be undone, and it refuses a table a sweep expired.
                if rerun:
                    recorded = self.tables.record_replacement(
                        before.name, result["columns"], result.get("truncated_to"),
                        owner=rerun.owner, title=rerun.title, sql=sql, max_rows=max_rows)
                else:
                    recorded = self.tables.record_replacement(before.name, result["columns"], result.get("truncated_to"))
                queries.swap_table(self.results_db, temp, before.name)
            except BaseException:
                if recorded is not None:
                    self.tables.restore_replacement(recorded)
                queries.drop_table(self.results_db, temp)
                self.tables[before.name]  # a sweep dropped the table meanwhile: TableExpired, the plain answer
                raise
            return Replaced(self.tables[before.name], result["row_count"])

    @contextmanager
    def _one_at_a_time(self, name: str) -> Iterator[None]:
        key = canonical(name)
        with self._lock:
            if key in self._busy:
                raise RefreshInProgress(f"{name} is already being refreshed.")
            self._busy.add(key)
        try:
            yield
        finally:
            with self._lock:
                self._busy.discard(key)
