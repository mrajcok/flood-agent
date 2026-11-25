"""The Notebook module: everything about Notebooks and their Cells. Creating, renaming and deleting Notebooks;
adding, copying, moving and deleting Cells; saving Drafts; Copy to Markdown Cell; deleting a Cell's earlier tables;
running and stopping a Cell, and which Cell is running in each Notebook (one at a time).

It raises `UnknownNotebook`, `UnknownCell` (both KeyErrors; a Cell of another Notebook is unknown) and
`NotebookBusy`, checked here for every operation that must not touch a running Cell. Notebook storage is used
through this module and the two Run modules only.
"""

from contextlib import aclosing
from dataclasses import dataclass, replace
from typing import AsyncGenerator, AsyncIterator, Literal, cast

from flood_agent import ai_cell_context, table_names
from flood_agent.ai_cell_run import AiCellRuns
from flood_agent.catalog import Catalog, canonical
from flood_agent.cell import (
    AiCell,
    AiRunSettings,
    CannotRun,
    Cell,
    Draft,
    MarkdownCell,
    MarkdownDraft,
    SqlCell,
    SqlRunSettings,
    kind_of,
    save_draft,
    starting_ai_draft,
    starting_sql_draft,
    typed_cell,
)
from flood_agent.cell_kinds import AI, MARKDOWN, SQL
from flood_agent.events import Event
from flood_agent.model_choices import ModelChoices
from flood_agent.notebook_store import COUNT_KIND as COUNT_KIND  # re-exported: the web layer does not import storage
from flood_agent.notebook_store import NotebookStore
from flood_agent.sql_cell_run import SqlCellRuns


class UnknownNotebook(KeyError):
    """No Notebook has this id."""


class UnknownCell(KeyError):
    """No Cell has this id in this Notebook (it may be another Notebook's)."""


class NotebookBusy(Exception):
    """A Cell in this Notebook is running; only one runs at a time, and a running Cell cannot be changed."""


# Whether deleting a Notebook or Cell also drops its live Result Tables (those still named
# rt_*). Its Saved Tables are always kept (ADR 0004).
TablesChoice = Literal["keep", "delete_rt"]

Position = Literal["after", "start", "end"]

TITLE_LENGTH = 60


@dataclass(frozen=True)
class PageCell:
    """A Cell as the page shows it. `run` is its latest Run's record (number, status, times, error, result, trace),
    None if it has not run."""

    cell: Cell
    run: dict | None
    stale: bool


@dataclass(frozen=True)
class NotebookPage:
    """A Notebook as its page shows it: its title and its Cells in order."""

    id: str
    title: str
    cells: tuple[PageCell, ...]

    @property
    def stale_ids(self) -> list[str]:
        """The ids of its Stale Cells, in order."""
        return [c.cell.id for c in self.cells if c.stale]


class Notebooks:
    def __init__(self, store: NotebookStore, tables: Catalog, sql_cells: SqlCellRuns, ai_cells: AiCellRuns,
                 choices: ModelChoices):
        self._store = store
        self._tables = tables
        self._sql_cells = sql_cells
        self._ai_cells = ai_cells
        self._choices = choices
        self._running: dict[str, str] = {}  # notebook id -> the Cell running in it now

    # --- Notebooks ----------------------------------------------------------------------------------------------

    def create_notebook(self, title: str | None = None) -> str:
        """A new, empty Notebook ("Notebook N" by default); returns its id."""
        self._tables.sweep()
        return self._store.create_notebook(title)

    def rename_notebook(self, notebook_id: str, title: str) -> None:
        """Retitle a Notebook, trimmed to 60 characters. ValueError if the title is blank."""
        title = title.strip()[:TITLE_LENGTH]
        if not title:
            raise ValueError("a notebook name cannot be blank")
        self._check_notebook(notebook_id)
        self._store.rename_notebook(notebook_id, title)

    def notebook_delete_preview(self, notebook_id: str) -> dict:
        """Counts for the delete dialog: the live Result Tables `delete_rt` would drop (any Cell's, any Run's) and the
        live Saved Tables that are always kept. `NotebookBusy` while a Cell runs."""
        tables = [t for c in self._idle_notebook(notebook_id) for t in self._tables.tables_of_cell(c["id"])]
        return _counts(tables)

    def delete_notebook(self, notebook_id: str, tables: TablesChoice = "keep") -> None:
        """Delete a Notebook with its Cells. `tables` says whether to drop the `rt_` tables its Runs made; Saved
        Tables are always kept. The tables go first, so a failure leaves the Notebook in place (ADR 0003, 0004).
        `NotebookBusy` while a Cell runs."""
        cells = self._idle_notebook(notebook_id)
        if tables == "delete_rt":
            for cell in cells:
                self._tables.drop_of_cell(cell["id"])
        self._store.delete_notebook(notebook_id)

    def list_notebooks(self, query: str = "") -> list[dict]:
        """The Notebooks ({id, title, created_at, last_activity_at}), most recent activity first. A non-blank `query`
        keeps those whose title or any Cell's Draft text contains it, ignoring case."""
        return self._store.list_notebooks(query)

    # --- reads for the page -------------------------------------------------------------------------------------

    def notebook_page(self, notebook_id: str) -> "NotebookPage":
        """The Notebook as its page shows it: its title and its typed Cells in order, each with its latest Run and
        whether it is Stale."""
        cells = self._known_notebook(notebook_id)
        return NotebookPage(notebook_id, self._store.title_of(notebook_id),
                            tuple(PageCell(self._typed(c), c["run"], stale) for c, stale in _with_stale(cells)))

    def stale_ids(self, notebook_id: str) -> list[str]:
        """The ids of the Notebook's Stale Cells, in order: `notebook_page(...).stale_ids`, without typing the Cells."""
        return [c["id"] for c, stale in _with_stale(self._known_notebook(notebook_id)) if stale]

    def page_cell(self, notebook_id: str, cell_id: str) -> "PageCell":
        """One Cell as the page shows it: typed, with its latest Run and whether it is Stale. Stale is worked out from
        the Cell and the Cells above it only, and agrees with `notebook_page`."""
        cells = self._known_notebook(notebook_id)
        index = next((i for i, c in enumerate(cells) if c["id"] == cell_id), None)
        if index is None:
            raise UnknownCell(cell_id)
        row = cells[index]
        return PageCell(self._typed(row), row["run"], ai_cell_context.is_stale(row, cells[:index]))

    def run_transcript(self, notebook_id: str, cell_id: str) -> list[dict]:
        """The transcript of the Cell's latest Run (its messages and Displays, in order); empty if it has not run."""
        run = self._row(notebook_id, cell_id)["run"]
        return self._store.transcript(run["id"]) if run else []

    def _typed(self, row: dict) -> Cell:
        return typed_cell(row, self._choices.default_model)

    # --- Cells --------------------------------------------------------------------------------------------------

    def cell(self, notebook_id: str, cell_id: str) -> Cell:
        """The Cell, typed: the default model (No model without an API key) stands in for a missing one."""
        return self._typed(self._row(notebook_id, cell_id))

    def add_cell(self, notebook_id: str, kind: str, position: Position = "end", after: str | None = None,
                 sql: str = "", draft: Draft | None = None) -> str:
        """Add a Cell of `kind` just after Cell `after` (`position` "after"), at the very top ("start") or at the
        end; returns its id. It starts with `draft` if given, else with the settings last Run for its kind: an AI
        Cell with that model and Effort if the model can still be used here (else the default model), a SQL Cell
        with that row cap and `sql` (Open as SQL Cell). ValueError for an unknown kind or a Draft of another kind."""
        starting = self._starting_draft(kind, sql, draft)
        cells = self._known_notebook(notebook_id)
        before = None
        if position == "after":
            if after is None or all(c["id"] != after for c in cells):
                raise UnknownCell(after)
        elif position == "start":
            after, before = None, cells[0]["id"] if cells else None
        else:
            after = None
        return self._store.add_cell(notebook_id, kind, starting.stored(), after, before)

    def _starting_draft(self, kind: str, sql: str, draft: Draft | None) -> Draft:
        """`draft` if it is of `kind`, else the Draft a new Cell of `kind` starts with. ValueError for an unknown
        kind or a Draft of another kind."""
        if draft is not None:
            if kind_of(draft) != kind:
                raise ValueError(f"a {kind_of(draft)} Draft cannot start a {kind} Cell")
            return draft
        if kind == AI:
            last, c = self._store.last_settings(AI), self._choices
            return starting_ai_draft(AiRunSettings.from_stored(last) if last else None, c.can_use, c.efforts_of,
                                     c.default_model)
        if kind == SQL:
            last = self._store.last_settings(SQL)
            return replace(starting_sql_draft(SqlRunSettings.from_stored(last) if last else None), sql=sql)
        if kind == MARKDOWN:
            return MarkdownDraft("", False, ())
        raise ValueError(f"unknown cell kind {kind!r}")

    def save_draft(self, notebook_id: str, cell_id: str, draft: Draft) -> None:
        """Save `draft` as the Cell's Draft, whole (even when it cannot be Run); its latest Run is untouched.
        ValueError if the Draft is not of the Cell's kind."""
        self._row(notebook_id, cell_id)
        save_draft(self._store, cell_id, draft)

    def copy_cell(self, notebook_id: str, cell_id: str) -> str:
        """Insert a copy of a Cell's Draft just below it and return its id. The copy has no Run."""
        cell = self._idle_cell(notebook_id, cell_id)
        return self._store.add_cell(notebook_id, cell["kind"], dict(cell["draft"]), after=cell_id)

    def move_cell(self, notebook_id: str, cell_id: str, direction: Literal["up", "down"]) -> None:
        """Swap a Cell with the one above or below it (nothing at either end)."""
        self._idle_cell(notebook_id, cell_id)
        self._store.move_cell(cell_id, direction)

    def cell_delete_preview(self, notebook_id: str, cell_id: str) -> dict:
        """Counts for the delete dialog: the live Result Tables `delete_rt` would drop (any Run's) and the live Saved
        Tables that are always kept."""
        self._idle_cell(notebook_id, cell_id)
        return _counts(self._tables.tables_of_cell(cell_id))

    def delete_cell(self, notebook_id: str, cell_id: str, tables: TablesChoice = "keep") -> None:
        """Delete a Cell. `tables` says whether to drop the `rt_` tables its Runs made; kept ones expire per their
        Retention, and Saved Tables are always kept. The tables go first, so a failure leaves the Cell in place and
        the delete can be retried (ADR 0003, 0004)."""
        self._idle_cell(notebook_id, cell_id)
        if tables == "delete_rt":
            self._tables.drop_of_cell(cell_id)
        self._store.delete_cell(cell_id)

    def delete_earlier_tables(self, notebook_id: str, cell_id: str) -> list[str]:
        """Drop the `rt_` tables of the Cell's earlier Runs (never Saved Tables, never its latest Run's) and return
        their names."""
        cell = self._idle_cell(notebook_id, cell_id)
        if cell["run"] is None:
            return []
        return self._tables.drop_earlier_of_cell(cell_id, cell["run"]["id"])

    # --- Copy to Markdown Cell ----------------------------------------------------------------------------------

    def _copyable_run(self, notebook_id: str, cell_id: str) -> tuple[str, list[dict]]:
        """An AI Cell's latest answer as a Markdown Cell would hold it: the text and the embedded Displays (a
        table or chart of a table that is still live, each with its own definition). Count Displays are not
        embedded: their number is already in the text. ValueError if the cell has not run."""
        cell = self._idle_cell(notebook_id, cell_id)
        if cell["kind"] != AI or cell["run"] is None:
            raise ValueError("Only an AI Cell that has run can be copied to a Markdown Cell.")
        texts: list[str] = []
        displays: list[dict] = []
        for item in self._store.transcript(cell["run"]["id"]):
            if item["type"] == "message":
                m = item["message"]
                if m["role"] == "assistant" and m.get("content"):
                    text = ai_cell_context.without_stop_note(m["content"])
                    if text:
                        texts.append(text)
            elif item["kind"] in ("table", "chart") and item["result_table"] in self._tables:
                d = {"kind": item["kind"], "table": item["result_table"]}
                if item["kind"] == "chart":
                    d["spec"] = item["spec"]
                displays.append(d)
        return "\n\n".join(texts), displays

    def markdown_copy_tables(self, notebook_id: str, cell_id: str) -> list[str]:
        """The `rt_` tables a Copy to Markdown Cell would embed, which the user may save first (so the copy does
        not expire with them)."""
        _, displays = self._copyable_run(notebook_id, cell_id)
        return list(dict.fromkeys(d["table"] for d in displays if table_names.is_result_table_name(d["table"])))

    def copy_to_markdown_cell(self, notebook_id: str, cell_id: str, save: dict[str, str] | None = None) -> str:
        """Insert a Markdown Cell below an AI Cell holding its answer text and one embedded Display per table or
        chart, and return its id. Send to AI starts unticked. `save` maps `rt_` tables of the answer to the names
        to rename them to (Saved Tables); every name is checked first, so a ValueError changes nothing."""
        save = save or {}
        usable = self.markdown_copy_tables(notebook_id, cell_id)
        if not set(save) <= set(usable):
            raise ValueError("Only the answer's rt_ tables can be saved.")
        for new in save.values():
            self._tables.check_new_name(new)
        if len({canonical(n) for n in save.values()}) != len(save):
            raise ValueError("Each saved table needs its own name.")
        for old, new in save.items():
            self._tables.update(old, new_name=new)
        text, displays = self._copyable_run(notebook_id, cell_id)
        draft = MarkdownDraft(text, False, tuple(displays))
        return self._store.add_cell(notebook_id, MARKDOWN, draft.stored(), after=cell_id)

    # --- Runs ---------------------------------------------------------------------------------------------------

    def running_cell(self, notebook_id: str) -> str | None:
        """The id of the Cell running in this Notebook now, or None."""
        return self._running.get(notebook_id)

    async def run_cell(self, notebook_id: str, cell_id: str) -> AsyncIterator[Event]:
        """Run a Cell with what its Draft holds and stream its events; its Run replaces the Cell's earlier one. The
        Notebook is claimed when this stream opens and released when it ends, however it ends: `NotebookBusy` if
        another Cell of the Notebook is running then. `CannotRun` (with its reason) if the Draft cannot be Run,
        before any Run is recorded. Closing the stream early stops the Run."""
        cell = self.cell(notebook_id, cell_id)
        reason = cell.cannot_run()
        if reason is not None:  # refused before a Run starts: the latest Run and its output stay
            raise CannotRun(reason)
        assert not isinstance(cell, MarkdownCell)  # never Run: refused just above
        if notebook_id in self._running:
            raise NotebookBusy("A cell in this notebook is already running.")
        self._running[notebook_id] = cell_id  # no await since the check: two streams cannot both claim it
        try:
            run = self._sql_cells.run(cell) if isinstance(cell, SqlCell) else self._ai_cells.run(cast(AiCell, cell))
            # Closed with this stream, so that an abandoned stream (the page was closed) stops the Run now.
            async with aclosing(cast(AsyncGenerator, run)) as events:
                async for item in events:
                    yield item
        finally:
            if self._running.get(notebook_id) == cell_id:
                del self._running[notebook_id]

    def stop_cell(self, notebook_id: str, cell_id: str) -> bool:
        """Stop a running Cell now. What it had streamed is kept and its Run is recorded as stopped. False if the Cell
        is not running."""
        self._row(notebook_id, cell_id)
        return self._sql_cells.stop(cell_id) or self._ai_cells.stop(cell_id)

    # --- Count Displays ------------------------------------------------------------------------------------------

    def count_display(self, display_id: int) -> dict | None:
        """A Run's Count Display definition ({title, value, sql, computed_at_ms}), or None if there is none with that id."""
        return self._store.count_display(display_id)

    def update_count(self, display_id: int, value: int, computed_at_ms: int) -> None:
        """Overwrite a Count Display's number and "as of" time (a Refresh); nothing of its Run changes. KeyError if it
        no longer exists."""
        self._store.update_count(display_id, value, computed_at_ms)

    # --- checks -------------------------------------------------------------------------------------------------

    def _check_notebook(self, notebook_id: str) -> None:
        """`UnknownNotebook` if there is no such Notebook."""
        if not self._store.exists(notebook_id):
            raise UnknownNotebook(notebook_id)

    def _known_notebook(self, notebook_id: str) -> list[dict]:
        """The Notebook's Cells as stored; `UnknownNotebook` if there is no such Notebook."""
        self._check_notebook(notebook_id)
        return self._store.cells(notebook_id)

    def _row(self, notebook_id: str, cell_id: str) -> dict:
        """The Cell as stored, if it is in the Notebook: `UnknownNotebook` or `UnknownCell` otherwise."""
        self._check_notebook(notebook_id)
        try:
            row = self._store.cell(cell_id)
        except KeyError:
            raise UnknownCell(cell_id) from None
        if row["notebook_id"] != notebook_id:
            raise UnknownCell(cell_id)
        return row

    def _idle_cell(self, notebook_id: str, cell_id: str) -> dict:
        """The Cell as stored, for an operation that cannot touch a running one: `NotebookBusy` if it is running."""
        row = self._row(notebook_id, cell_id)
        if self._running.get(notebook_id) == cell_id:
            raise NotebookBusy("This cell is running; stop it first")
        return row

    def _idle_notebook(self, notebook_id: str) -> list[dict]:
        """The Notebook's Cells, for an operation that cannot touch a running one: `NotebookBusy` if a Cell runs."""
        cells = self._known_notebook(notebook_id)
        if notebook_id in self._running:
            raise NotebookBusy("A cell in this notebook is running; stop it first")
        return cells


def _with_stale(cells: list[dict]) -> list[tuple[dict, bool]]:
    """Each stored Cell with whether it is Stale, worked out from it and the Cells above it."""
    return [(c, ai_cell_context.is_stale(c, cells[:i])) for i, c in enumerate(cells)]


def _counts(tables: list) -> dict:
    return {"result": sum(t.kind == "result" for t in tables), "saved": sum(t.kind == "saved" for t in tables)}
