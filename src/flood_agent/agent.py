"""The Agent: runs the LLM tool loop for one AI Cell Run and emits an event stream.

The HTTP/SSE layer is a thin adapter over `Agent.run_cell`. The LLM client is injected.
"""

import asyncio
import hashlib
import json
import logging
import sys
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, AsyncIterator, Callable, Iterator, Literal, Protocol

from mcp import StdioServerParameters
from mcp.types import TextContent

from flood_agent import flagging, queries, table_names
from flood_agent.catalog import Catalog, CellRun, Owner, Table, TableExpired, canonical
from flood_agent.llm import LLMClient, LLMError, ModelSpec, TextChunk, ToolCallChunk, UsageChunk
from flood_agent.mcp_connection import McpConnection, Transport, stdio_transport
from flood_agent.mcp_protocol import QUERY_TOOLS
from flood_agent.mcp_server import ServerSettings
from flood_agent.notebooks import AI, COUNT_KIND, DEFAULT_MAX_ROWS, MARKDOWN, MARKDOWN_NOTE_LABEL, SQL, NotebookStore
from flood_agent.semantic_layer import SemanticLayer
from flood_agent.show_commands import SHOW_IN_TEXTBOX, ShowFailed, is_show_statement, run_show, statement_of
from flood_agent.trace import TraceBuilder, context_tokens
from flood_agent.upgrade import drop_pre_notebook_data

log = logging.getLogger("uvicorn.error")  # the app's terminal log

SHOW_ROWS_TO_AI = 100  # how many rows of a SHOW Cell's result an AI Cell below is sent


class NotRefreshable(Exception):
    """The table has no stored query, so Refresh cannot run it."""


class RefreshInProgress(Exception):
    """The table is already being refreshed."""


class NotebookBusy(Exception):
    """A cell in this notebook is already running; only one runs at a time."""


class RefreshFailed(Exception):
    """A Refresh did not complete; the message is plain text safe to show the user."""


# Whether deleting a Notebook or Cell also drops its live Result Tables (those still named
# rt_*). Its Saved Tables are always kept (ADR 0004).
TablesChoice = Literal["keep", "delete_rt"]


@dataclass
class AgentConfig:
    """Built from `config.Settings`, which holds every default."""

    telemetry_db: Path
    results_db: Path
    app_db: Path
    semantic_layer: Path
    model: str  # the default model: runs a turn that names none
    models: tuple[ModelSpec, ...]  # the curated list; window_of looks a model's window up here
    result_ttl_ms: int  # Result Tables expire this long after they are made
    query_timeout_s: float
    chart_row_cap: int
    max_failed_attempts: int
    max_llm_steps: int
    clock: Callable[[], int] = lambda: int(time.time() * 1000)  # epoch ms; injectable for tests
    fixed_now_ms: int | None = None  # pins SQL now() in the MCP server process (evals); None is the wall clock

    def spec_of(self, model_id: str) -> ModelSpec | None:
        """The curated model with this id; None for an id outside the list (NO_MODEL included)."""
        return next((m for m in self.models if m.id == model_id), None)

    def window_of(self, model: str) -> int:
        """The context window of a curated model; 0 for an id outside the list (NO_MODEL: no context window)."""
        spec = self.spec_of(model)
        return spec.context_window if spec else 0


NO_MODEL = "no-model"
"""The Model Choice id for Fake Mode: a prompt sent with it is answered by the scripted stand-in."""

ClientFactory = Callable[[str], LLMClient]
"""Gives the client that runs a model id: the real OpenRouter client (made lazily, reused) or a scripted fake."""


class ExchangeSink(Protocol):
    """Where one LLM exchange is stored: its messages, Displays and Trace, and the owner its Result Tables
    are recorded against. An AI Cell's Run is one."""

    @property
    def owner(self) -> Owner: ...
    @property
    def key(self) -> str: ...  # names the exchange (the cell id)
    @property
    def session_id(self) -> str: ...  # lets OpenRouter route every step of the exchange to one cache

    def record(self, history: list[dict], message: dict) -> int: ...
    def add_step(self, items: list[tuple[dict, list[tuple]]]) -> tuple[list[int], list[list[int]]]: ...
    def save_trace(self, trace: dict) -> None: ...


class _CellSink:
    """Stores an AI Cell's Run in the NotebookStore."""

    def __init__(self, store: NotebookStore, notebook_id: str, cell_id: str, run_id: str):
        self.store, self.run_id = store, run_id
        self.owner = CellRun(cell_id, run_id)
        self.key = cell_id
        self.session_id = f"notebook-{notebook_id}"  # one cache for every cell of a notebook

    def record(self, history: list[dict], message: dict) -> int:
        history.append(message)
        return self.store.add_message(self.run_id, message)

    def add_step(self, items):
        return self.store.add_step(self.run_id, items)

    def save_trace(self, trace: dict) -> None:
        self.store.save_trace(self.run_id, trace, context_tokens(trace))


DAY_MS = 24 * 3600 * 1000


@dataclass
class TextDelta:
    text: str


class _TurnQueue(asyncio.Queue):
    """A turn's event queue that remembers whether the reply so far ends mid-paragraph: in
    text, not after a newline or a Display (which the page shows as its own block)."""

    mid_paragraph = False

    def put_nowait(self, item) -> None:  # `put` goes through here too
        if isinstance(item, TextDelta) and item.text:
            self.mid_paragraph = not item.text.endswith("\n")
        elif isinstance(item, (DisplayEvent, CountEvent)):
            self.mid_paragraph = False
        super().put_nowait(item)

    def paragraph_break(self) -> str:
        """What to put before text that starts a new paragraph: a blank line, if needed."""
        return "\n\n" if self.mid_paragraph else ""

    async def put_notice(self, message: str) -> None:
        """Stream a notice from the app (not the LLM) as its own paragraph."""
        await self.put(TextDelta(self.paragraph_break() + message))


@dataclass
class DisplayEvent:
    kind: str
    result_table: str
    columns: list[str] = field(default_factory=list)
    spec: dict | None = None  # charts: Chart Spec after data-reference resolution
    source_spec: dict | None = None  # charts: Chart Spec as the LLM wrote it (for the Trace)


@dataclass
class CountEvent:
    """A Count Display: the number a successful run_count_query returned, shown as its own one-line item.
    `sql` is kept (also in the saved definition) so a later Refresh can run it again."""

    title: str
    value: int
    sql: str
    computed_at_ms: int
    display_id: int | None = None  # the saved Display, which a Refresh names


@dataclass
class TraceEntry:
    """Trace data for ticket #10 to pick up. `query_error` carries the raw SQLite error,
    which is never sent to the LLM, next to the sanitised error the LLM did see."""

    kind: str
    sql: str | None = None
    raw_error: str | None = None
    sanitized_error: str | None = None


@dataclass
class TraceReady:
    """The finished Trace for the turn (already persisted); for the UI only, never for the LLM."""

    trace: dict


@dataclass
class FlagEvent:
    """Tokens in a completed reply that the LLM could not have known. For the analyst only
    (reply note + Trace); never sent to the LLM."""

    tokens: list[str]
    items: list[dict] = field(default_factory=list)  # [{"type": "ip"|"number", "token": ...}]


@dataclass
class LlmCallStarted:
    """The turn is about to make its `number`th LLM call (1-based); for the live view only."""

    number: int


@dataclass
class TraceLive:
    """A Trace entry, the moment it is added to the turn's Trace; for the live view only."""

    entry: dict


@dataclass
class TurnError:
    """The turn failed partway. `message` was already streamed to the user (and saved); `detail`
    is the technical description (also in the Trace) for harnesses such as the eval runner."""

    message: str
    detail: str


@dataclass
class TurnEnd:
    pass


Event = TextDelta | DisplayEvent | CountEvent | TraceEntry | FlagEvent | TraceReady | LlmCallStarted | TraceLive | TurnError | TurnEnd

GIVE_UP_MESSAGE = (
    "Sorry, I couldn't build a working query for that after several attempts. "
    "Please try rephrasing your question."
)


STOPPED_NOTE = "*Stopped by user.*"


@dataclass
class _LiveStep:
    """The LLM step in progress, so that a stop can keep what it had already streamed."""

    text: str = ""
    system_prompt: str = ""
    started: float = 0.0
    in_trace: bool = False  # the Trace already has this step
    open: bool = False  # started, and not yet saved to the history


STEP_LIMIT_MESSAGE = (
    "Sorry, I wasn't able to finish that within the allowed number of steps. "
    "Please try a simpler or more specific question."
)


def _fail(message: str) -> str:
    return json.dumps({"success": False, "error": message})


def _unwrap(e: BaseException) -> BaseException:
    while isinstance(e, BaseExceptionGroup) and e.exceptions:
        e = e.exceptions[0]  # the MCP client wraps the real failure in a TaskGroup
    return e


def repair_history(messages: list[dict]) -> list[dict]:
    """Drop assistant tool_calls messages that lack a tool result for every call id (and the
    partial results after them), and orphan tool messages, so a corrupt saved history never
    makes the provider reject every later request."""
    out: list[dict] = []
    i = 0
    while i < len(messages):
        m = messages[i]
        if m["role"] == "assistant" and m.get("tool_calls"):
            j = i + 1
            answered = set()
            while j < len(messages) and messages[j]["role"] == "tool":
                answered.add(messages[j].get("tool_call_id"))
                j += 1
            if {tc["id"] for tc in m["tool_calls"]} <= answered:
                out.extend(messages[i:j])
            i = j
        elif m["role"] == "tool":
            i += 1
        else:
            out.append(m)
            i += 1
    return out


def resolve_chart_data(source: dict, table: str) -> dict:
    resolved = _resolve_data(source, table, {"url": f"/api/results/{table}/chart-rows"})
    assert isinstance(resolved, dict)  # a dict in gives a dict out
    return resolved


def _resolve_data(node, table: str, url: dict):
    """Copy of the Chart Spec with every {"name": table} data reference replaced by the URL."""
    if isinstance(node, dict):
        return {
            k: (dict(url) if k == "data" and v == {"name": table} else _resolve_data(v, table, url))
            for k, v in node.items()
        }
    if isinstance(node, list):
        return [_resolve_data(i, table, url) for i in node]
    return node


def markdown_note(draft: dict) -> str:
    """What an AI Cell is sent for a ticked Markdown Cell: its text, then one line per embedded Display naming
    its kind and table. Never rows or a chart's data (ADR 0001); the same draft always gives the same note."""
    lines = [f"{MARKDOWN_NOTE_LABEL} {draft.get('text', '').strip()}".rstrip()]
    lines += [f"[Display: {d['kind']} {d['table']}]" for d in draft.get("displays") or []]
    return "\n".join(lines)


class Agent:
    def __init__(self, config: AgentConfig, client_for: ClientFactory):
        self.config = config
        self.client_for = client_for
        self.semantic_layer = SemanticLayer(config.semantic_layer)
        drop_pre_notebook_data(config.app_db, config.results_db)  # before anything creates the new schema
        self.notebooks = NotebookStore(config.app_db, config.clock)
        self.tables = Catalog(
            config.app_db, config.clock, config.result_ttl_ms, telemetry_tables=self.semantic_layer.table_names(),
            results_db=config.results_db, on_rename=self._follow_rename,
        )
        self._connection: McpConnection | None = None
        self._restart_lock = asyncio.Lock()
        self._turn_tasks: set[asyncio.Task] = set()
        self._keyed_turns: dict[str, asyncio.Task] = {}  # running cell runs by cell id
        self._stopped_turns: set[asyncio.Task] = set()  # turns `stop_turn` cancelled
        self._running_cells: dict[str, str] = {}  # notebook id -> the cell running in it now
        self._sql_stops: dict[str, threading.Event] = {}  # SQL Cell id -> its running query's stop flag
        self._refreshing: set[tuple[str, str | int]] = set()  # ("table", name) or ("count", id) being refreshed now
        self._refreshing_lock = threading.Lock()
        self.tables.sweep()

    async def start(self) -> None:
        """Launch the long-lived MCP server. Needed before `run_cell`, not for row fetches
        or PSV export."""
        if self._connection is not None:
            raise RuntimeError("Agent already started")
        connection = McpConnection(self._transport())
        await connection.start()
        self._connection = connection

    async def _ensure_connection(self) -> McpConnection:
        """The live connection; a dead one (process exited or session broken) is replaced first."""
        async with self._restart_lock:
            connection = self._connection
            if connection is None:
                raise RuntimeError("Agent not started")
            if not connection.alive:
                replacement = McpConnection(self._transport())
                await connection.aclose()  # reap the dead one
                await replacement.start()
                self._connection = replacement
                connection = replacement
            return connection

    async def aclose(self) -> None:
        """Stop the MCP server and reap its process."""
        # In-flight turns first (this also cancels a restart in progress), so none is left
        # talking to a closing connection.
        tasks = list(self._turn_tasks)
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        async with self._restart_lock:
            connection, self._connection = self._connection, None
            if connection is not None:
                await connection.aclose()

    async def __aenter__(self) -> "Agent":
        await self.start()
        return self

    async def __aexit__(self, *exc) -> None:
        await self.aclose()

    def _server_settings(self) -> ServerSettings:
        # Static settings only. The row cap travels in each call's `_meta`, not here
        # and not as a tool argument, so the LLM can't choose it.
        return ServerSettings(
            telemetry_db=self.config.telemetry_db,
            results_db=self.config.results_db,
            app_db=self.config.app_db,
            query_timeout_s=self.config.query_timeout_s,
            chart_row_cap=self.config.chart_row_cap,
            telemetry_tables=tuple(self.semantic_layer.table_names()),
            fixed_now_ms=self.config.fixed_now_ms,
        )

    def _server_params(self) -> StdioServerParameters:
        return StdioServerParameters(
            command=sys.executable, args=["-m", "flood_agent.mcp_server"], env=self._server_settings().to_env()
        )

    def _transport(self) -> Transport:
        """How a new connection reaches its server: a stdio subprocess (tests swap in an in-process one)."""
        return stdio_transport(self._server_params())

    # --- Notebooks (AI Cells only so far) ---------------------------------------------------------------

    def create_notebook(self, title: str | None = None) -> str:
        """Persist a new, empty Notebook ("Notebook N" by default) and return its id."""
        self.tables.sweep()
        return self.notebooks.create_notebook(title)

    def add_ai_cell(
        self, notebook_id: str, prompt: str = "", model: str | None = None, effort: str | None = None,
        max_rows: int = DEFAULT_MAX_ROWS, after: str | None = None, before: str | None = None,
    ) -> str:
        """Add an AI Cell (after cell `after`, or at the end) whose Draft holds its prompt and settings."""
        return self.notebooks.add_ai_cell(
            notebook_id, {"prompt": prompt, "model": model, "effort": effort, "max_rows": max_rows}, after, before)

    def add_sql_cell(
        self, notebook_id: str, sql: str = "", max_rows: int = DEFAULT_MAX_ROWS, after: str | None = None,
        before: str | None = None, command: str = "", table: str = "",
    ) -> str:
        """Add a SQL Cell (after cell `after`, or at the end) whose Draft holds its SQL and row cap. A `command`
        (a SHOW statement or DESCRIBE TABLE, with its `table`) makes it a SHOW Cell: the SQL is not used."""
        return self.notebooks.add_cell(
            notebook_id, SQL, {"sql": sql, "max_rows": max_rows, "command": command, "table": table}, after, before)

    def _follow_rename(self, con, old: str, new: str) -> None:
        """Every Display, in a Run or a Markdown Cell, follows a table's new name."""
        self.notebooks.follow_rename(con, old, new)

    def _copyable_run(self, notebook_id: str, cell_id: str) -> tuple[str, list[dict]]:
        """An AI Cell's latest answer as a Markdown Cell would hold it: the text and the embedded Displays (a
        table or chart of a table that is still live, each with its own definition). Count Displays are not
        embedded: their number is already in the text. ValueError if the cell has not run."""
        cell = self._idle_cell(notebook_id, cell_id)
        if cell["kind"] != AI or cell["run"] is None:
            raise ValueError("Only an AI Cell that has run can be copied to a Markdown Cell.")
        texts: list[str] = []
        displays: list[dict] = []
        for item in self.notebooks.transcript(cell["run"]["id"]):
            if item["type"] == "message":
                m = item["message"]
                if m["role"] == "assistant" and m.get("content"):
                    text = m["content"].removesuffix(STOPPED_NOTE).rstrip()
                    if text:
                        texts.append(text)
            elif item["kind"] in ("table", "chart") and item["result_table"] in self.tables:
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
            self.tables.check_new_name(new)
        if len({canonical(n) for n in save.values()}) != len(save):
            raise ValueError("Each saved table needs its own name.")
        for old, new in save.items():
            self.tables.update(old, new_name=new)
        text, displays = self._copyable_run(notebook_id, cell_id)
        return self.notebooks.add_cell(
            notebook_id, MARKDOWN, {"text": text, "send_to_ai": False, "displays": displays}, after=cell_id)

    def add_markdown_cell(self, notebook_id: str, text: str = "", send_to_ai: bool = False,
                          after: str | None = None, before: str | None = None) -> str:
        """Add a Markdown Cell. Its text reaches the AI Cells below only while `send_to_ai` is ticked."""
        return self.notebooks.add_cell(notebook_id, MARKDOWN, {"text": text, "send_to_ai": send_to_ai}, after, before)

    def _idle_cell(self, notebook_id: str, cell_id: str) -> dict:
        """The cell, for an operation that cannot touch a running one: KeyError if it is not in the notebook,
        `NotebookBusy` if it is running."""
        cell = self.notebooks.cell(cell_id)
        if cell["notebook_id"] != notebook_id:
            raise KeyError(cell_id)
        if self.running_cell(notebook_id) == cell_id:
            raise NotebookBusy("This cell is running; stop it first")
        return cell

    def rename_notebook(self, notebook_id: str, title: str) -> None:
        """Retitle a notebook (at most 60 characters). ValueError if the title is blank, KeyError if unknown."""
        title = title.strip()[:60]
        if not title:
            raise ValueError("a notebook name cannot be blank")
        self.notebooks.rename_notebook(notebook_id, title)

    def _idle_notebook(self, notebook_id: str) -> list[dict]:
        """The notebook's cells, for an operation that cannot touch a running one: KeyError if unknown,
        `NotebookBusy` if a cell is running."""
        if not self.notebooks.exists(notebook_id):
            raise KeyError(notebook_id)
        if self.running_cell(notebook_id):
            raise NotebookBusy("A cell in this notebook is running; stop it first")
        return self.notebooks.cells(notebook_id)

    def notebook_delete_preview(self, notebook_id: str) -> dict:
        """Counts for the delete dialog: the live Result Tables `delete_rt` would drop (any cell's, any Run's)
        and the live Saved Tables that are always kept."""
        tables = [t for c in self._idle_notebook(notebook_id) for t in self.tables.tables_of_cell(c["id"])]
        return {"result": sum(t.kind == "result" for t in tables), "saved": sum(t.kind == "saved" for t in tables)}

    def delete_notebook(self, notebook_id: str, tables: TablesChoice = "keep") -> None:
        """Delete a notebook with its cells. `tables` (keep or delete_rt) says whether to drop the `rt_` tables its
        Runs made; Saved Tables are always kept. The tables go first, so a failure leaves the notebook in place."""
        cells = self._idle_notebook(notebook_id)
        if tables == "delete_rt":
            for cell in cells:
                self.tables.drop_of_cell(cell["id"])
        self.notebooks.delete_notebook(notebook_id)

    def move_cell(self, notebook_id: str, cell_id: str, direction: Literal["up", "down"]) -> None:
        """Swap a cell with the one above or below it (nothing at either end). Staleness follows from the new order."""
        self._idle_cell(notebook_id, cell_id)
        self.notebooks.move_cell(cell_id, direction)

    def copy_cell(self, notebook_id: str, cell_id: str) -> str:
        """Insert a copy of a cell's Draft just below it and return its id: an AI Cell's prompt and settings, a SQL
        Cell's SQL and row cap, a Markdown Cell's text and Send to AI. The copy has no Run."""
        cell = self._idle_cell(notebook_id, cell_id)
        return self.notebooks.add_cell(notebook_id, cell["kind"], dict(cell["draft"]), after=cell_id)

    def cell_delete_preview(self, notebook_id: str, cell_id: str) -> dict:
        """Counts for the delete dialog: the live Result Tables `delete_rt` would drop (any Run's) and the live
        Saved Tables that are always kept."""
        self._idle_cell(notebook_id, cell_id)
        tables = self.tables.tables_of_cell(cell_id)
        return {"result": sum(t.kind == "result" for t in tables), "saved": sum(t.kind == "saved" for t in tables)}

    def delete_cell(self, notebook_id: str, cell_id: str, tables: TablesChoice = "keep") -> None:
        """Delete a cell. `tables` (keep or delete_rt) says whether to drop the `rt_` tables its Runs made; kept
        ones expire per their Retention. Saved Tables are always kept. The tables go first, so a failure leaves
        the cell in place and the delete can be retried."""
        self._idle_cell(notebook_id, cell_id)
        if tables == "delete_rt":
            self.tables.drop_of_cell(cell_id)
        self.notebooks.delete_cell(cell_id)

    async def run_cell(self, notebook_id: str, cell_id: str) -> AsyncIterator[Event]:
        """Run a cell, with what its Draft holds, and stream its events (`start()` first). A SQL Cell runs its
        SQL without the LLM (see `_run_sql_cell`); an AI Cell as follows.

        It is sent the system prompt, then the AI Cells above it in order (each as its latest Run stored it:
        prompt, answer, tool calls and results), then its own prompt; cells below are never sent. Its Run
        replaces the cell's earlier one. `NotebookBusy` if a cell of this notebook is already running,
        KeyError if the cell is not in the notebook."""
        if self._connection is None:
            raise RuntimeError("Agent not started: call `await agent.start()` or use `async with agent`")
        cell = self.notebooks.cell(cell_id)
        if cell["notebook_id"] != notebook_id:
            raise KeyError(cell_id)
        if notebook_id in self._running_cells:
            raise NotebookBusy("A cell in this notebook is already running.")
        self._running_cells[notebook_id] = cell_id
        if cell["kind"] == SQL:
            try:
                async for item in self._run_sql_cell(notebook_id, cell):
                    yield item
            finally:
                if self._running_cells.get(notebook_id) == cell_id:
                    del self._running_cells[notebook_id]
            return
        try:
            task, queue, release = self._start_cell_run(notebook_id, cell)
        except BaseException:
            del self._running_cells[notebook_id]
            raise
        try:
            async for item in self._consume(task, queue):
                yield item
        finally:
            release()  # now, not whenever the task's done callback gets its turn

    def _context_above(self, notebook_id: str, cell_id: str) -> tuple[list[dict], list[tuple[str, str]]]:
        """What an AI Cell is sent from the cells above it, built from what each ran (never from Drafts), plus
        the (cell id, run id) of each AI Cell above that has run. Shared by a Run and by stale detection."""
        history: list[dict] = []
        above: list[tuple[str, str]] = []
        for other in self.notebooks.cells(notebook_id):
            if other["id"] == cell_id:
                break
            if other["kind"] == MARKDOWN:
                text = other["draft"].get("text", "").strip()
                if other["draft"].get("send_to_ai") and (text or other["draft"].get("displays")):
                    history.append({"role": "user", "content": markdown_note(other["draft"])})
            elif other["run"]:
                history += self._what_ran(other["run"])
                above.append((other["id"], other["run"]["id"]))
        return history, above

    @staticmethod
    def _fingerprint(history: list[dict]) -> str:
        return hashlib.sha256(json.dumps(history, sort_keys=True).encode()).hexdigest()

    def stale_cells(self, notebook_id: str) -> set[str]:
        """The ids of the notebook's AI Cells that are stale: the context each would be sent now differs from
        the one its latest Run was sent. SQL Cells (which store no fingerprint), cells that never ran and cells
        still running are never stale."""
        stale = set()
        for cell in self.notebooks.cells(notebook_id):
            run = cell["run"]
            if cell["kind"] == AI and run and run["status"] != "running" and run["context_fingerprint"]:
                if self._fingerprint(self._context_above(notebook_id, cell["id"])[0]) != run["context_fingerprint"]:
                    stale.add(cell["id"])
        return stale

    def _start_cell_run(
        self, notebook_id: str, cell: dict,
    ) -> tuple[asyncio.Task, _TurnQueue, Callable[[], None]]:
        cell_id, draft = cell["id"], cell["draft"]
        history, above = self._context_above(notebook_id, cell_id)
        fingerprint = self._fingerprint(history)
        settings = {"model": draft.get("model") or self.config.model, "effort": draft.get("effort"),
                    "max_rows": draft.get("max_rows") or DEFAULT_MAX_ROWS}
        run_id = self.notebooks.start_run(cell_id, draft["prompt"], settings, fingerprint)
        sink = _CellSink(self.notebooks, notebook_id, cell_id, run_id)

        def prompt_tables() -> list[Table]:
            tables = [t for c, r in above for t in self.tables.tables_of_run(c, r)]
            tables += self.tables.tables_of_run(cell_id, run_id)
            return sorted(tables, key=lambda t: (t.created_at_ms, t.name))

        queue = _TurnQueue()
        task = asyncio.create_task(self._produce_cell(
            history, prompt_tables, sink, draft["prompt"], queue, settings))
        self._turn_tasks.add(task)
        task.add_done_callback(self._turn_tasks.discard)
        self._keyed_turns[cell_id] = task

        def release(_t: asyncio.Task | None = None) -> None:
            """The cell is no longer running: free the notebook, and close a Run the task could not."""
            if self._keyed_turns.get(cell_id) is task:
                del self._keyed_turns[cell_id]
            if self._running_cells.get(notebook_id) == cell_id:
                del self._running_cells[notebook_id]
            if task.done() and self.notebooks.status_of(run_id) == "running":  # cancelled or crashed first
                self.notebooks.finish_run(run_id, "stopped" if task in self._stopped_turns else "failed")
        task.add_done_callback(release)
        return task, queue, release

    @staticmethod
    def _what_ran(run: dict) -> list[dict]:
        """A finished Run's messages as the cells below are sent them: a stopped or failed Run counts as having
        run, with what it streamed plus "(stopped by the user)" or "failed: <value-free error>"."""
        messages = repair_history(run["messages"])
        if run["status"] not in ("stopped", "failed"):
            return messages
        last = messages[-1] if messages and messages[-1]["role"] == "assistant" else None
        text = (last.get("content") or "") if last else ""
        if run["status"] == "stopped":
            note = "(stopped by the user)"
            text = text.removesuffix(STOPPED_NOTE).rstrip()
        else:
            errors = [e["message"] for e in (run["trace"] or {}).get("entries", []) if e["kind"] == "error"]
            note = f"failed: {errors[0] if errors else 'the Run did not finish'}"
            if errors and text == errors[0]:
                text = ""  # the error message was recorded as the answer: say it once
        if last:
            messages = messages[:-1]
        return messages + [{"role": "assistant", "content": f"{text}\n\n{note}" if text else note}]

    async def _produce_cell(
        self, history: list[dict], prompt_tables: Callable[[], list[Table]], sink: _CellSink, prompt: str,
        queue: _TurnQueue, settings: dict,
    ):
        def end(trace: dict) -> TraceReady:
            kinds = {e["kind"] for e in trace["entries"]}
            self.notebooks.finish_run(sink.run_id, "stopped" if "stopped" in kinds else "failed" if "error" in kinds else "ok")
            return TraceReady(trace)

        try:
            sink.record(history, {"role": "user", "content": prompt})
            await self._exchange(history, prompt_tables, sink, prompt, queue, settings["max_rows"],
                                 settings["model"], settings["effort"], end=end)
            await queue.put(TurnEnd())
        except asyncio.CancelledError:
            raise
        except BaseException as e:  # surfaced to the consumer
            await queue.put(e)

    # --- SQL Cells --------------------------------------------------------------------------------------

    async def _run_sql_cell(self, notebook_id: str, cell: dict) -> AsyncIterator[Event]:
        """Run a SQL Cell's statement without the LLM, through the query path Refresh uses. A bare count gives
        a Count Display; anything else becomes a table (a re-run swaps the new rows in under the same name). The
        Run keeps the labelled message an AI Cell below is sent, with its Query Metadata as it is now."""
        cell_id, draft = cell["id"], cell["draft"]
        sql, max_rows = draft.get("sql", ""), draft.get("max_rows") or DEFAULT_MAX_ROWS
        command, table = draft.get("command") or "", draft.get("table") or ""
        statement = statement_of(command, table) if command else sql
        prior = (self.tables.tables_of_cell(cell_id) or [None])[-1]  # the table a re-run replaces, if still live
        run_id = self.notebooks.start_run(cell_id, statement, {"max_rows": max_rows, "command": command}, "")
        stop = self._sql_stops[cell_id] = threading.Event()
        started = time.monotonic()
        try:
            try:
                outcome: dict[str, Any] = await asyncio.to_thread(self._execute_sql, sql, max_rows, stop, prior, CellRun(cell_id, run_id), command, table)
            except asyncio.CancelledError:  # the client left: abandon the query
                stop.set()
                self.notebooks.finish_run(run_id, "stopped")
                raise
            except Exception:
                log.exception("SQL Cell %s crashed", cell_id)
                msg = "The query could not be run."
                outcome = {"status": "failed", "error": msg, "raw_error": msg}
        finally:
            self._sql_stops.pop(cell_id, None)
        outcome["elapsed_s"] = round(time.monotonic() - started, 3)
        label = f"[SHOW Cell, run by the user] {statement}" if command else f"[SQL Cell, run by the user] {sql}"
        displays: list[tuple] = []
        event: Event | None = None
        if outcome["status"] == "stopped":
            detail = "Stopped by the user."
        elif outcome["status"] == "failed":
            detail = f"Failed: {outcome['error']}"
        elif "rows" in outcome:
            detail = self._show_message(outcome["columns"], outcome["rows"])
        elif "count" in outcome:
            detail = f"Count: {outcome['count']}"
            computed, title = self.config.clock(), queries.shorten_sql(sql)
            displays.append((COUNT_KIND, None, {"title": title, "value": outcome["count"], "sql": sql, "computed_at_ms": computed}))
            event = CountEvent(title, outcome["count"], sql, computed)
        else:
            meta = {"result_table": outcome["table"], "columns": outcome["columns"], "row_count": outcome["row_count"]}
            if outcome.get("truncated_to"):
                meta["truncated_to"] = outcome["truncated_to"]
            detail = f"Query Metadata: {json.dumps(meta)}"
            names = [c["name"] for c in outcome["columns"]]
            displays.append(("table", outcome["table"], {"columns": names}))
            event = DisplayEvent("table", outcome["table"], names)
        # The message is stored once, so what AI Cells below are sent is frozen at this Run.
        self.notebooks.add_step(run_id, [({"role": "user", "content": f"{label}\n{detail}"}, displays)])
        self.notebooks.save_result(run_id, {k: v for k, v in outcome.items() if k != "columns" or "rows" in outcome})
        self.notebooks.finish_run(run_id, outcome["status"])
        if event is not None:
            yield event
        if outcome["status"] == "failed":
            yield TurnError(outcome["raw_error"], "sql cell failed")
        yield TurnEnd()

    @staticmethod
    def _show_message(columns: list[str], rows: list[list]) -> str:
        """A SHOW Cell result as an AI Cell below is sent it: the columns and up to SHOW_ROWS_TO_AI rows, with a
        note when rows were left out."""
        lines = [" | ".join(columns)] + [" | ".join("" if v is None else str(v) for v in r) for r in rows[:SHOW_ROWS_TO_AI]]
        if len(rows) > SHOW_ROWS_TO_AI:
            lines.append(f"(first {SHOW_ROWS_TO_AI} of {len(rows)} rows; the other rows were left out)")
        return "\n".join(lines)

    def _execute_sql(self, sql: str, max_rows: int, stop: threading.Event, prior: Table | None, owner: CellRun,
                     command: str = "", table: str = "") -> dict:
        """The blocking part of a SQL Cell Run (it runs in a thread): the outcome as a dict with `status` ok, failed
        or stopped, then `count`, or `table`, `columns`, `row_count` and `truncated_to`, or `error` (value-free) and
        `raw_error` (for the user, and logged)."""
        cfg, readable = self.config, self.tables.readable()
        if command:  # SHOW / DESCRIBE: no table, no row cap; the rows are kept with the Run
            try:
                names, rows = run_show(command, table, cfg.telemetry_db, cfg.results_db, self.tables.listing())
            except ShowFailed as e:
                log.warning("SQL Cell %s failed: %s", owner.cell_id, e)
                return {"status": "failed", "error": str(e), "raw_error": str(e)}
            return {"status": "ok", "columns": names, "rows": rows, "row_count": len(rows), "command": command}
        if is_show_statement(sql):
            return {"status": "failed", "error": SHOW_IN_TEXTBOX, "raw_error": SHOW_IN_TEXTBOX}

        def failure(result: dict) -> dict:
            if result.get("stopped"):
                return {"status": "stopped"}
            raw = result.get("raw_error") or result["error"]
            log.warning("SQL Cell %s failed: %s", owner.cell_id, raw)
            return {"status": "failed", "error": result["error"], "raw_error": raw}

        if queries.looks_like_count(sql):
            counted = queries.run_count_query(cfg.telemetry_db, cfg.results_db, sql, cfg.query_timeout_s, readable, stop)
            if counted["success"]:
                return {"status": "ok", "count": counted["count"]}
            if counted.get("stopped") or counted["error"] != queries.COUNT_GUIDANCE:
                return failure(counted)  # a real failure; only the wrong shape falls back to a table
        title = queries.shorten_sql(sql)
        result = queries.run_query(cfg.telemetry_db, cfg.results_db, sql, cfg.query_timeout_s, max_rows, readable, title, stop)
        if not result["success"]:
            return failure(result)
        temp = name = result["result_table"]
        before = None
        if prior is not None:
            try:  # Catalog first (it refuses a table a sweep has expired), then the atomic swap
                before = self.tables.record_refresh(prior.name, result["columns"], result.get("truncated_to"))
            except TableExpired:
                prior = None
        try:
            if prior is None:
                self.tables.register_for(owner, temp, title, result["columns"], table_names.SQL_CELL_REQUEST,
                                         result.get("truncated_to"), sql, max_rows)
            else:
                queries.swap_table(cfg.results_db, temp, prior.name)
                name = prior.name
                self.tables.reown_for_rerun(name, owner, title, sql, max_rows)
        except BaseException:
            if before is not None:
                self.tables.restore_refresh(before)
            queries.drop_table(cfg.results_db, temp)
            raise
        return {"status": "ok", "table": name, "columns": result["columns"], "row_count": result["row_count"],
                "truncated_to": result.get("truncated_to")}

    def running_cell(self, notebook_id: str) -> str | None:
        """The id of the cell running in this notebook now, or None."""
        return self._running_cells.get(notebook_id)

    def stop_cell(self, cell_id: str) -> bool:
        """Stop a running cell now. What it had streamed is kept and its Run is recorded as stopped.
        False if the cell is not running."""
        flag = self._sql_stops.get(cell_id)
        if flag is not None:  # a SQL Cell: its query's progress handler sees the flag and aborts it
            flag.set()
            return True
        return self.stop_turn(cell_id)

    def fetch_rows(self, result_table: str, columns: list[str] | None = None, **page_args) -> dict:
        """Internal data path used by the row-JSON endpoint. Not an LLM tool.

        Adds per-column display formats from the Semantic Layer."""
        table = self.tables[result_table]
        data = queries.fetch_rows(self.config.results_db, table.name, columns, **page_args)
        data["formats"] = self.semantic_layer.column_formats(data["columns"])
        data["truncated_to"] = table.truncated_to
        data.update(table.to_json())
        return data

    def refresh_table(self, name: str) -> Table:
        """Refresh a live Result or Saved Table: run its stored query again, without the LLM, and replace
        it under the same name (ADR 0006). The new rows are built under a temporary name and swapped in,
        so any failure leaves the table, its rows and its catalog row as they were.

        KeyError if unknown, `TableExpired` if no longer live, `NotRefreshable` if it has no stored query,
        `RefreshInProgress` if it is already being refreshed, `RefreshFailed` (with a plain message) if the
        query is rejected or fails. A plain call, not an MCP tool: no rows reach the LLM (ADR 0001)."""
        table = self.tables[name]
        if table.sql is None or table.max_rows is None:
            raise NotRefreshable(f"{table.name} has no stored query, so it cannot be refreshed.")
        with self._refresh_guard(("table", table.name), table.name):
            result = queries.run_query(
                self.config.telemetry_db, self.config.results_db, table.sql, self.config.query_timeout_s,
                table.max_rows, self.tables.readable(), table.title,
            )
            if not result["success"]:
                raise RefreshFailed(self._refresh_error(table.name, result))
            temp = result["result_table"]
            before = None
            try:
                # Catalog first: it is the one step that can be undone, and it refuses a table a sweep has
                # expired. Then the swap, which is atomic.
                before = self.tables.record_refresh(table.name, result["columns"], result.get("truncated_to"))
                queries.swap_table(self.config.results_db, temp, table.name)
                return self.tables[table.name]
            except BaseException:
                if before is not None:
                    self.tables.restore_refresh(before)
                queries.drop_table(self.config.results_db, temp)
                self.tables[table.name]  # a sweep dropped the table meanwhile: TableExpired, the plain answer
                raise

    @contextmanager
    def _refresh_guard(self, key: tuple[str, str | int], label: str) -> Iterator[None]:
        """Allow one Refresh at a time per `key`; `RefreshInProgress` (naming `label`) for a second."""
        with self._refreshing_lock:
            if key in self._refreshing:
                raise RefreshInProgress(f"{label} is already being refreshed.")
            self._refreshing.add(key)
        try:
            yield
        finally:
            with self._refreshing_lock:
                self._refreshing.discard(key)

    @staticmethod
    def _refresh_error(what: str, result: dict) -> str:
        """Log a failed Refresh query and return the plain message to show: the sanitised error the LLM path
        uses, with a table that has expired since put in words."""
        log.warning("refresh of %s failed: %s", what, result.get("raw_error") or result["error"])
        error = result["error"]
        if error == queries.ACCESS_DENIED or "no such table" in error:  # a table it read has expired
            error = "A table this query reads has expired or been deleted. Ask again to regenerate it."
        return error

    def refresh_count(self, display_id: int) -> dict:
        """Refresh a Count Display: run its stored count query again, without the LLM, under the same checks
        and timeout as the original, and overwrite its number and "as of" time. Returns the new
        {"value", "computed_at_ms"}. KeyError if unknown, `RefreshInProgress` if already refreshing,
        `RefreshFailed` (plain message) if the query fails, in which case the old number is kept.
        A Refresh is never a Run."""
        definition = self.notebooks.count_display(display_id)
        if definition is None:
            raise KeyError(display_id)
        label = f'Count Display "{definition["title"]}"'
        with self._refresh_guard(("count", display_id), label):
            result = queries.run_count_query(
                self.config.telemetry_db, self.config.results_db, definition["sql"], self.config.query_timeout_s,
                self.tables.readable(),
            )
            if not result["success"]:
                raise RefreshFailed(self._refresh_error(label, result))
            computed_at_ms = self.config.clock()
            self.notebooks.update_count(display_id, result["count"], computed_at_ms)
            return {"value": result["count"], "computed_at_ms": computed_at_ms}

    def iter_all_rows(self, result_table: str):
        """Internal data path for PSV export of the full Result Table. Not an LLM tool."""
        return queries.iter_all_rows(self.config.results_db, self.tables[result_table].name)

    def fetch_chart_rows(self, result_table: str) -> list[dict]:
        """Internal data path for the chart-rows endpoint (chart cap). Not an LLM tool."""
        return queries.fetch_records(self.config.results_db, self.tables[result_table].name, self.config.chart_row_cap)

    @staticmethod
    async def _consume(task: asyncio.Task, queue: _TurnQueue) -> AsyncIterator[Event]:
        """Yield a producer task's events until its end. If the consumer stops early, the task is cancelled
        and awaited."""
        finished = False
        try:
            while True:
                item = await queue.get()
                if isinstance(item, BaseException):
                    finished = True
                    raise item
                finished = isinstance(item, TurnEnd)
                yield item
                if finished:
                    break
        finally:
            if not finished:
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    def stop_turn(self, turn_key: str) -> bool:
        """Stop a running turn now. What it had streamed is kept (history, Trace) and its events end
        normally. False if no such turn is running."""
        task = self._keyed_turns.get(turn_key)
        if task is None or task.done():
            return False
        self._stopped_turns.add(task)
        task.cancel()
        return True

    async def _exchange(
        self, history: list[dict], prompt_tables: Callable[[], list[Table]], sink: ExchangeSink, request: str,
        queue: _TurnQueue, max_result_rows: int, model: str, effort: str | None, end: Callable[[dict], object],
    ):
        """The one path an LLM exchange runs through: `history` (already ending in the user's message, extended
        in place), `prompt_tables` (the Result Tables the system prompt lists, asked before every step), and
        `sink` (where messages, Displays, Trace and new Result Tables are recorded, whatever owns them)."""
        trace = TraceBuilder(model, on_add=lambda entry: queue.put_nowait(TraceLive(entry)))
        live = _LiveStep()
        try:
            await self._converse(history, prompt_tables, sink, request, trace, queue, max_result_rows, model, effort, live)
        except asyncio.CancelledError:
            if asyncio.current_task() not in self._stopped_turns:  # the client left or the app is closing
                sink.save_trace(trace.finish())
                raise
            self._record_stop(history, sink, live, trace, queue)
        except Exception as e:
            await self._fail_turn(history, sink, trace, queue, e)
        finished = trace.finish()
        sink.save_trace(finished)
        await queue.put(end(finished))

    def _record_stop(self, history, sink: ExchangeSink, live: _LiveStep, trace: TraceBuilder, queue: _TurnQueue) -> None:
        """The user stopped the turn: keep the text of the step in progress, and say so, in the page,
        the history and the Trace. A step cut off before its tool calls were answered keeps only its text."""
        partial = live.text if live.open else ""
        if live.open and not live.in_trace:
            trace.llm_step(live.system_prompt, live.text, [], None, live.started)
        trace.add("stopped")
        message = (partial.rstrip() + "\n\n" if partial.strip() else "") + STOPPED_NOTE
        sink.record(history, {"role": "assistant", "content": message})
        queue.put_nowait(TextDelta(queue.paragraph_break() + STOPPED_NOTE))

    async def _fail_turn(self, history, sink: ExchangeSink, trace: TraceBuilder, queue: _TurnQueue, error: BaseException) -> None:
        """A turn failed partway: tell the user, record it in the Trace and in the history."""
        error = _unwrap(error)
        detail = f"{type(error).__name__}: {error}"
        if isinstance(error, LLMError):
            message = f"Sorry, I couldn't get a complete answer from the model. {error}"
        else:
            message = f"Sorry, something went wrong while handling that request ({type(error).__name__}). Please try again."
        trace.add("error", message=message, detail=detail)
        sink.record(history, {"role": "assistant", "content": message})
        await queue.put_notice(message)
        await queue.put(TurnError(message, detail))

    async def _converse(
        self, history: list[dict], prompt_tables: Callable[[], list[Table]], sink: ExchangeSink, user_message: str,
        trace: TraceBuilder, queue: _TurnQueue, max_result_rows: int, model: str, effort: str | None = None, live: _LiveStep | None = None,
    ):
        live = live or _LiveStep()
        llm = self.client_for(model)
        spec = self.config.spec_of(model)
        # Only a non-default Effort is sent; the default sends nothing extra.
        reasoning = {"effort": effort} if effort and spec and effort != spec.default_effort else None
        session_id = sink.session_id  # the client decides per model whether to send it
        connection = await self._ensure_connection()
        tools = connection.tools
        failed_attempts = 0
        for call_number in range(1, self.config.max_llm_steps + 1):
            system_prompt = self.semantic_layer.system_prompt(prompt_tables())
            messages = [{"role": "system", "content": system_prompt}]
            messages += history
            text, calls, usage = "", [], None
            step_started = time.monotonic()
            live.text, live.system_prompt, live.started, live.in_trace, live.open = "", system_prompt, step_started, False, True
            queue.put_nowait(LlmCallStarted(call_number))
            async for chunk in llm.stream_chat(messages, tools, reasoning=reasoning, session_id=session_id):
                if isinstance(chunk, TextChunk):
                    # Each step's text is a new paragraph on the page, as it is when the turn is reloaded.
                    shown = chunk.text if text or not chunk.text else queue.paragraph_break() + chunk.text
                    text += chunk.text
                    live.text = text
                    await queue.put(TextDelta(shown))
                elif isinstance(chunk, ToolCallChunk):
                    calls.append(chunk)
                elif isinstance(chunk, UsageChunk):
                    usage = chunk
            sent_bytes = len(json.dumps({"messages": messages, "tools": tools}, default=str).encode())
            trace.llm_step(system_prompt, text, calls, usage, step_started, sent_bytes)
            live.in_trace = True
            await self._flag_unseen_values(text, system_prompt, messages, calls, trace, queue)
            assistant: dict = {"role": "assistant", "content": text}
            if calls:
                assistant["tool_calls"] = [
                    {
                        "id": c.id,
                        "type": "function",
                        "function": {"name": c.name, "arguments": c.arguments},
                    }
                    for c in calls
                ]
            # The assistant message and every tool result are saved together after all of the
            # step's calls have been answered, so a failure partway never leaves history with
            # an unanswered tool_call.
            items: list[tuple[dict, list[tuple]]] = [(assistant, [])]
            # The step's Display events in call order, sent once the step is saved (a Count Display needs its
            # id): (event, index in items of the message its Display is saved after).
            step_displays: list[tuple[DisplayEvent | CountEvent, int]] = []
            for c in calls:
                call_started = time.monotonic()
                content, args, executed_sql, failed = await self._run_tool(
                    connection, c, queue, trace, sink, user_message, max_result_rows
                )
                failed_attempts += failed
                trace.tool_call(c.name, args, content, call_started, executed_sql)
                self._trace_result_table(trace, content)
                displays, events = self._prepare_displays(content, args, trace)
                step_displays += [(e, len(items)) for e in events]
                count = self._count_display(c.name, content, args)
                if count:
                    displays.append((COUNT_KIND, None, {"title": count.title, "value": count.value, "sql": count.sql,
                                                     "computed_at_ms": count.computed_at_ms}))
                    step_displays.append((count, len(items)))
                items.append(({"role": "tool", "tool_call_id": c.id, "content": content}, displays))
            _, display_ids = sink.add_step(items)
            for event, index in step_displays:
                if isinstance(event, CountEvent):
                    event.display_id = display_ids[index][-1]  # a call makes one Display: its count is the last
                await queue.put(event)
            history.extend(m for m, _ in items)
            live.open = False
            if not calls:
                break
            if failed_attempts >= self.config.max_failed_attempts:
                await self._end_with_notice(history, sink, GIVE_UP_MESSAGE, queue)
                break
        else:
            trace.add("step_limit", max_steps=self.config.max_llm_steps)
            await self._end_with_notice(history, sink, STEP_LIMIT_MESSAGE, queue)

    async def _end_with_notice(self, history: list[dict], sink: ExchangeSink, message: str, queue: _TurnQueue) -> None:
        sink.record(history, {"role": "assistant", "content": message})
        await queue.put_notice(message)

    async def _run_tool(
        self, connection: McpConnection, c: ToolCallChunk, queue: asyncio.Queue, trace: TraceBuilder,
        sink: ExchangeSink, request: str, max_result_rows: int,
    ) -> tuple[str, dict, str | None, int]:
        """Execute one LLM tool call. Returns (content, args, executed_sql, failed 0/1).

        Never raises for problems the LLM caused (bad JSON, missing `sql`) or tool failures: it
        returns an error result the LLM can act on, and a failed query tool call counts toward the limit."""
        is_query = c.name in QUERY_TOOLS
        args: dict = {}
        executed_sql = None
        try:
            parsed = json.loads(c.arguments or "{}")
        except ValueError:
            parsed = None
            content = _fail("The tool arguments were not valid JSON. Send a JSON object and try again.")
        else:
            if not isinstance(parsed, dict):
                content = _fail("The tool arguments must be a JSON object. Try again.")
            elif is_query and not isinstance(parsed.get("sql"), str):
                args = parsed
                content = _fail(f"{c.name} requires a 'sql' string argument. Try again.")
            else:
                args = parsed
                try:
                    result = await connection.call_tool(c.name, args, max_result_rows)
                except Exception as e:  # a broken tool call must not corrupt or abort the turn
                    trace.add("tool_error", name=c.name, detail=f"{type(_unwrap(e)).__name__}: {_unwrap(e)}")
                    return _fail("The tool failed to run. Try again."), args, None, 1
                content = "".join(b.text for b in result.content if isinstance(b, TextContent))
                try:
                    # Straight after the call returns, before any other await, so a turn stopped here
                    # still has its table registered (and so listed and swept).
                    self._register_result_table(sink.owner, content, args, request, max_result_rows)
                except Exception as e:  # the tool ran; only recording its table failed
                    trace.add("registration_error", name=c.name, detail=f"{type(e).__name__}: {e}")
                    return _fail("The query ran but its Result Table could not be recorded. Try again."), args, None, 1
                if result.isError:
                    content = _fail(content[:500] or "The tool call was rejected.")
        if is_query:
            content, executed_sql = self._strip_executed_sql(content)
            content, failed = await self._split_query_error(content, args, queue, trace)
            return content, args, executed_sql, failed
        return content, args, None, 0

    @staticmethod
    async def _flag_unseen_values(
        text: str, system_prompt: str, messages: list[dict], calls: list[ToolCallChunk], trace, queue
    ) -> None:
        """Flag IP-like / large-number tokens in a completed reply that the LLM was never sent.

        Allowed context = what this step's LLM call received, except the LLM's own earlier prose
        (so repeating an invented value is flagged again): system prompt (Semantic Layer, Result
        Tables), user messages, tool results (Query Metadata), and tool-call SQL arguments, including
        those of this same step (a time bound it states and queries with is its own filter)."""
        if not text:
            return
        allowed = [system_prompt]
        for m in messages:
            if m["role"] in ("user", "tool"):
                allowed.append(m["content"] or "")
            elif m["role"] == "assistant":
                allowed += [c["function"]["arguments"] for c in m.get("tool_calls") or []]
        allowed += [c.arguments for c in calls]
        found = flagging.find_unseen(text, allowed)
        if not found:
            return
        items = [{"type": t, "token": tok} for t, tok in found]
        for item in items:
            trace.add("flag", **item)
        await queue.put(FlagEvent([i["token"] for i in items], items))

    def _register_result_table(
        self, owner: Owner, content: str, args: dict, request: str, max_result_rows: int
    ) -> None:
        """Record the Result Table a successful query tool call made, with the owner that made it."""
        try:
            payload = json.loads(content)
        except ValueError:
            return
        if isinstance(payload, dict) and payload.get("success") and payload.get("result_table") and "display" not in payload:
            title = args.get("title", "")
            self.tables.register_for(
                owner, payload["result_table"], title if isinstance(title, str) else "",
                payload.get("columns", []), request, payload.get("truncated_to"), args.get("sql"), max_result_rows,
            )

    @staticmethod
    def _strip_executed_sql(content: str) -> tuple[str, str | None]:
        """The CTAS actually executed is for the Trace only; remove it from the LLM's metadata."""
        try:
            meta = json.loads(content)
        except ValueError:
            return content, None
        if not isinstance(meta, dict) or "executed_sql" not in meta:
            return content, None
        executed = meta.pop("executed_sql")
        return json.dumps(meta), executed

    @staticmethod
    def _trace_result_table(trace: TraceBuilder, content: str) -> None:
        try:
            meta = json.loads(content)
        except ValueError:
            return
        if isinstance(meta, dict) and meta.get("success") and "display" not in meta and meta.get("result_table"):
            trace.result_table(meta["result_table"], meta.get("row_count"), meta.get("columns"))

    async def _split_query_error(
        self, content: str, args: dict, queue, trace: TraceBuilder | None = None
    ) -> tuple[str, int]:
        """Strip the raw SQLite error from run_query / run_count_query output before the LLM sees it, and
        emit it (with the sanitised error) as a Trace entry. Returns (content, failed 0/1)."""
        try:
            meta = json.loads(content)
        except ValueError:
            return content, 0
        if not isinstance(meta, dict) or meta.get("success", True):
            return content, 0
        raw = meta.pop("raw_error", None)
        await queue.put(TraceEntry("query_error", args.get("sql"), raw, meta.get("error")))
        if trace:
            trace.add("query_error", sql=args.get("sql"), raw_error=raw, sanitized_error=meta.get("error"))
        return json.dumps(meta), 1

    def _prepare_displays(
        self, tool_content: str, args: dict, trace: TraceBuilder,
    ) -> tuple[list[tuple], list[DisplayEvent]]:
        """The Display a tool call made: the definitions [(kind, result_table, definition)] to persist with
        the tool message, and the events to send the user once the step is saved."""
        try:
            payload = json.loads(tool_content)
        except ValueError:
            return [], []
        if not isinstance(payload, dict):
            return [], []
        display = payload.get("display")
        if not display:
            return [], []
        table = display["result_table"]
        # Any live Result Table may be shown (ADR 0003); the Display is saved with this
        # Cell, but the table stays owned by the one that created it.
        live = table in self.tables
        if display["kind"] == "chart":
            source = display["spec"]
            resolved = resolve_chart_data(source, table)
            trace.add("chart_spec", result_table=table, source_spec=source, spec=resolved)
            event = DisplayEvent("chart", table, spec=resolved, source_spec=source)
            return ([("chart", table, {"spec": source})] if live else []), [event]
        if not live:
            return [], []
        return ([(display["kind"], table, {"columns": display["columns"]})],
                [DisplayEvent(display["kind"], table, display["columns"])])

    def _count_display(self, tool: str, content: str, args: dict) -> CountEvent | None:
        """A successful run_count_query makes a Count Display (shown once it is saved). It has no result
        table (so no Retention) and lives and dies with its Run."""
        if tool != "run_count_query":
            return None
        try:
            payload = json.loads(content)
        except ValueError:
            return None
        if not isinstance(payload, dict) or not payload.get("success") or not isinstance(payload.get("count"), int):
            return None
        title = args.get("title")
        return CountEvent(title if isinstance(title, str) else "", payload["count"], args["sql"], self.config.clock())
