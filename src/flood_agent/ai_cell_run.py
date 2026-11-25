"""AI Cell Runs: everything an AI Cell's Run does, from starting the Run down.

A Run sends the LLM the system prompt, the AI Cell context above the Cell and its prompt, then runs the LLM step
loop: each step's text is streamed, its tool calls are run on the MCP server, and their outcomes become Result
Tables, Displays and Count Displays. Values in a reply the LLM was never sent are flagged. The loop ends with an
answer, a give-up or step-limit notice, a stop (the user's, or the stream dropped), or a failure.

It records the Run in the NotebookStore (its messages, Displays and Trace) and its Result Tables in the Table
Catalog against the Cell and Run.
"""

import asyncio
import json
import time
from contextlib import aclosing
from dataclasses import dataclass
from typing import AsyncGenerator, AsyncIterator, Awaitable, Callable

from mcp.types import TextContent

from flood_agent import ai_cell_context, flagging
from flood_agent.catalog import Catalog, CellRun, Table
from flood_agent.cell import AiCell
from flood_agent.events import (
    CountEvent,
    DisplayEvent,
    Event,
    FlagEvent,
    LlmCallStarted,
    TextDelta,
    TraceEntry,
    TraceLive,
    TraceReady,
    TurnEnd,
    TurnError,
)
from flood_agent.llm import LLMClient, LLMError, ModelSpec, TextChunk, ToolCallChunk, UsageChunk
from flood_agent.mcp_connection import McpConnection
from flood_agent.mcp_protocol import QUERY_TOOLS
from flood_agent.notebook_store import COUNT_KIND, NotebookStore, RunStatus
from flood_agent.semantic_layer import SemanticLayer
from flood_agent.tool_outcome import Counted, DisplayMade, Failed, Outcome, ResultTableMade, Stopped, decode, project
from flood_agent.trace import TraceBuilder, context_tokens

ClientFactory = Callable[[str], LLMClient]
"""Gives the client that runs a model id: the real OpenRouter client (made lazily, reused) or a scripted fake."""

ConnectionProvider = Callable[[], Awaitable[McpConnection]]
"""Gives the live MCP connection, restarting a dead server first (the Agent's, ADR 0002)."""


class _RunQueue(asyncio.Queue):
    """A Run's event queue that remembers whether the reply so far ends mid-paragraph: in
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


GIVE_UP_MESSAGE = (
    "Sorry, I couldn't build a working query for that after several attempts. "
    "Please try rephrasing your question."
)


@dataclass
class _LiveStep:
    """The LLM step in progress, so that a stop can keep what it had already streamed."""

    text: str = ""
    system_prompt: str = ""
    started: float = 0.0
    in_trace: bool = False  # the Trace already has this step
    open: bool = False  # started, and not yet saved to the history


@dataclass(frozen=True)
class _ActiveRun:
    """The Run being produced, and what it runs with: its Cell and Run ids, its prompt, the Result Tables its
    system prompt lists (asked before every step), the session that routes its LLM calls to one cache, the
    queue its events stream through, and the settings it was started with."""

    cell_run: CellRun
    prompt: str
    prompt_tables: Callable[[], list[Table]]
    session_id: str
    queue: _RunQueue
    model: str
    effort: str | None
    max_rows: int


RUN_CRASHED = "The Run did not finish."  # a failed Run's reason when its task crashed without closing it

STEP_LIMIT_MESSAGE = (
    "Sorry, I wasn't able to finish that within the allowed number of steps. "
    "Please try a simpler or more specific question."
)


def _unwrap(e: BaseException) -> BaseException:
    while isinstance(e, BaseExceptionGroup) and e.exceptions:
        e = e.exceptions[0]  # the MCP client wraps the real failure in a TaskGroup
    return e


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


class AiCellRuns:
    """Runs AI Cells and stops them. Built once by the Agent; the Notebook module keeps the rule that one Cell runs
    per Notebook at a time, and the Agent supervises the MCP connection this gets from `connection`."""

    def __init__(
        self, store: NotebookStore, tables: Catalog, semantic_layer: SemanticLayer, client_for: ClientFactory,
        connection: ConnectionProvider, started: Callable[[], bool], spec_of: Callable[[str], ModelSpec | None],
        max_llm_steps: int, max_failed_attempts: int, clock: Callable[[], int],
    ):
        self._store, self.tables, self.semantic_layer = store, tables, semantic_layer
        self.client_for, self._connection, self._started = client_for, connection, started
        self.spec_of = spec_of
        self.max_llm_steps, self.max_failed_attempts, self.clock = max_llm_steps, max_failed_attempts, clock
        self.tasks: set[asyncio.Task] = set()  # every running Run's producer task
        self._keyed: dict[str, asyncio.Task] = {}  # running Runs by cell id
        self._closed = False

    async def run(self, cell: AiCell) -> AsyncIterator[Event]:
        """Run an AI Cell with what its Draft holds and stream its events.

        It is sent the system prompt, then the AI Cells above it in order (each as its latest Run stored it:
        prompt, answer, tool calls and results), then its own prompt; cells below are never sent. Its Run
        replaces the cell's earlier one. If the stream is abandoned (the client left), the Run is stopped as
        the user's stop does (what it had streamed is kept) and waited for. RuntimeError after `aclose`."""
        if self._closed:
            raise RuntimeError("AI Cell Runs are closed")
        if not self._started():  # before the Run is recorded
            raise RuntimeError("Agent not started: call `await agent.start()` or use `async with agent`")
        task, queue, release = self._start(cell)
        try:
            async with aclosing(self._consume(task, queue)) as events:  # closed with this stream, not later
                async for item in events:
                    yield item
        finally:
            release()  # now, not whenever the task's done callback gets its turn

    def stop(self, cell_id: str) -> bool:
        """Stop a running AI Cell now. What it had streamed is kept (history, Trace) and its events end
        normally. False if the Cell is not running here."""
        task = self._keyed.get(cell_id)
        if task is None or task.done():
            return False
        task.cancel()
        return True

    async def aclose(self) -> None:
        """Cancel every running Run and wait for it, so none is left using the MCP connection. No Run starts
        after this."""
        self._closed = True
        tasks = list(self.tasks)
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    def _start(self, cell: AiCell) -> tuple[asyncio.Task, _RunQueue, Callable[[], None]]:
        notebook_id, cell_id, prompt, settings = cell.notebook_id, cell.id, cell.prompt, cell.run_settings
        context = ai_cell_context.context_above(self._store.cells(notebook_id), cell_id)
        history, above, fingerprint = context.messages, context.above, context.fingerprint
        run_id = self._store.start_run(cell_id, prompt, settings.stored(), fingerprint)
        run = CellRun(cell_id, run_id)
        session_id = f"notebook-{notebook_id}"  # lets OpenRouter route every step of every cell to one cache

        def prompt_tables() -> list[Table]:
            return ai_cell_context.listed_result_tables(above, cell_id, run_id, self.tables.tables_of_run)

        queue = _RunQueue()
        active = _ActiveRun(run, prompt, prompt_tables, session_id, queue, settings.model, settings.effort,
                            settings.max_rows)
        task = asyncio.create_task(self._produce(history, active))
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)
        self._keyed[cell_id] = task

        def release(_t: asyncio.Task | None = None) -> None:
            """The cell is no longer running: close a Run the task could not. A task cancelled before it could
            close its Run (before its first step, say) was stopped, as every cancellation stops a Run; one that
            ended any other way without closing it crashed."""
            if self._keyed.get(cell_id) is task:
                del self._keyed[cell_id]
            if task.done() and self._store.status_of(run_id) == "running":
                if task.cancelled():
                    self._store.finish_run(run_id, "stopped")
                else:
                    self._store.finish_run(run_id, "failed", RUN_CRASHED)

        def end_stream(_t: asyncio.Task) -> None:
            if task.cancelled():  # it could not end its events itself, so its stream is not left waiting
                queue.put_nowait(TurnEnd())
        task.add_done_callback(release)
        task.add_done_callback(end_stream)
        return task, queue, release

    async def _produce(self, history: list[dict], active: _ActiveRun):
        try:
            self._record(history, active.cell_run, {"role": "user", "content": active.prompt})
            await self._run_llm_steps(history, active)
            await active.queue.put(TurnEnd())
        except asyncio.CancelledError:
            raise
        except BaseException as e:  # surfaced to the consumer
            await active.queue.put(e)

    @staticmethod
    async def _consume(task: asyncio.Task, queue: _RunQueue) -> AsyncGenerator[Event]:
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

    async def _run_llm_steps(self, history: list[dict], active: _ActiveRun):
        """The one path a Run's LLM steps run through: `history` (already ending in the user's message, extended
        in place), and `active`, the Run being produced. Its messages, Displays and Trace are stored with its
        CellRun, and its Result Tables recorded against it.

        It closes the Run with how the loop ended, then streams the finished Trace: ok (an answer, including a
        give-up or step-limit notice), stopped (the user's stop, the client leaving or the app closing), or failed
        with the value-free message the user was shown."""
        run, queue = active.cell_run, active.queue
        trace = TraceBuilder(active.model, on_add=lambda entry: queue.put_nowait(TraceLive(entry)))
        live = _LiveStep()
        status: RunStatus = "ok"
        error: str | None = None
        try:
            await self._converse(history, active, trace, live)
        except asyncio.CancelledError:  # the user's stop, the client leaving or the app closing: all stop the Run
            self._record_stop(history, active, live, trace)
            status = "stopped"
        except Exception as e:
            status, error = "failed", await self._record_failure(history, active, trace, e)
        finished = trace.finish()
        self._save_trace(run, finished)
        self._store.finish_run(run.run_id, status, error)
        await queue.put(TraceReady(finished))

    def _record(self, history: list[dict], run: CellRun, message: dict) -> None:
        history.append(message)
        self._store.add_message(run.run_id, message)

    def _save_trace(self, run: CellRun, trace: dict) -> None:
        self._store.save_trace(run.run_id, trace, context_tokens(trace))

    def _record_stop(self, history: list[dict], active: _ActiveRun, live: _LiveStep, trace: TraceBuilder) -> None:
        """The Run was stopped: keep the text of the step in progress, and say so, in the page,
        the history and the Trace. A step cut off before its tool calls were answered keeps only its text."""
        partial = live.text if live.open else ""
        if live.open and not live.in_trace:
            trace.llm_step(live.system_prompt, live.text, [], None, live.started)
        trace.add("stopped")
        message = (partial.rstrip() + "\n\n" if partial.strip() else "") + ai_cell_context.STOPPED_NOTE
        self._record(history, active.cell_run, {"role": "assistant", "content": message})
        active.queue.put_nowait(TextDelta(active.queue.paragraph_break() + ai_cell_context.STOPPED_NOTE))

    async def _record_failure(
        self, history: list[dict], active: _ActiveRun, trace: TraceBuilder, error: BaseException,
    ) -> str:
        """The Run failed partway: tell the user, record it in the Trace and in the history. Returns the
        value-free message the user was shown."""
        error = _unwrap(error)
        detail = f"{type(error).__name__}: {error}"
        if isinstance(error, LLMError):
            message = f"Sorry, I couldn't get a complete answer from the model. {error}"
        else:
            message = f"Sorry, something went wrong while handling that request ({type(error).__name__}). Please try again."
        trace.add("error", message=message, detail=detail)
        self._record(history, active.cell_run, {"role": "assistant", "content": message})
        await active.queue.put_notice(message)
        await active.queue.put(TurnError(message, detail))
        return message

    async def _converse(self, history: list[dict], active: _ActiveRun, trace: TraceBuilder, live: _LiveStep):
        queue, model, effort = active.queue, active.model, active.effort
        llm = self.client_for(model)
        spec = self.spec_of(model)
        # Only a non-default Effort is sent; the default sends nothing extra.
        reasoning = {"effort": effort} if effort and spec and effort != spec.default_effort else None
        connection = await self._connection()
        tools = connection.tools
        failed_attempts = 0
        for call_number in range(1, self.max_llm_steps + 1):
            system_prompt = self.semantic_layer.system_prompt(active.prompt_tables())
            messages = [{"role": "system", "content": system_prompt}]
            messages += history
            text, calls, usage = "", [], None
            step_started = time.monotonic()
            live.text, live.system_prompt, live.started, live.in_trace, live.open = "", system_prompt, step_started, False, True
            queue.put_nowait(LlmCallStarted(call_number))
            async for chunk in llm.stream_chat(messages, tools, reasoning=reasoning, session_id=active.session_id):
                if isinstance(chunk, TextChunk):
                    # Each step's text is a new paragraph on the page, as it is when the Run is reloaded.
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
                content, args, executed_sql, failed, outcome = await self._run_tool(connection, c, trace, active)
                failed_attempts += failed
                trace.tool_call(c.name, args, content, call_started, executed_sql)
                displays, events = [], []
                if isinstance(outcome, ResultTableMade):
                    trace.result_table(outcome.result_table, outcome.row_count, outcome.columns)
                elif isinstance(outcome, DisplayMade):
                    displays, events = self._prepare_displays(outcome, trace)
                step_displays += [(e, len(items)) for e in events]
                count = self._count_display(c.name, outcome, args)
                if count:
                    displays.append((COUNT_KIND, None, {"title": count.title, "value": count.value, "sql": count.sql,
                                                     "computed_at_ms": count.computed_at_ms}))
                    step_displays.append((count, len(items)))
                items.append(({"role": "tool", "tool_call_id": c.id, "content": content}, displays))
            _, display_ids = self._store.add_step(active.cell_run.run_id, items)
            for event, index in step_displays:
                if isinstance(event, CountEvent):
                    event.display_id = display_ids[index][-1]  # a call makes one Display: its count is the last
                await queue.put(event)
            history.extend(m for m, _ in items)
            live.open = False
            if not calls:
                break
            if failed_attempts >= self.max_failed_attempts:
                await self._end_with_notice(history, active, GIVE_UP_MESSAGE)
                break
        else:
            trace.add("step_limit", max_steps=self.max_llm_steps)
            await self._end_with_notice(history, active, STEP_LIMIT_MESSAGE)

    async def _end_with_notice(self, history: list[dict], active: _ActiveRun, message: str) -> None:
        self._record(history, active.cell_run, {"role": "assistant", "content": message})
        await active.queue.put_notice(message)

    async def _run_tool(
        self, connection: McpConnection, c: ToolCallChunk, trace: TraceBuilder, active: _ActiveRun,
    ) -> tuple[str, dict, str | None, int, Outcome | None]:
        """Execute one LLM tool call. Returns (content for the LLM, args, executed_sql, failed 0/1, outcome).

        A tool's result is decoded once into an `Outcome` (None only when the call itself broke): `content` is
        its projection, and everything else the Run needs comes from the outcome. Never raises for problems the LLM caused (bad JSON, missing `sql`) or tool failures: it
        returns an error result the LLM can act on, and a failed query tool call counts toward the limit."""
        is_query = c.name in QUERY_TOOLS
        args: dict = {}
        outcome: Outcome | None = None
        try:
            parsed = json.loads(c.arguments or "{}")
        except ValueError:
            parsed = None
            outcome = Failed("The tool arguments were not valid JSON. Send a JSON object and try again.")
        else:
            if not isinstance(parsed, dict):
                outcome = Failed("The tool arguments must be a JSON object. Try again.")
            elif is_query and not isinstance(parsed.get("sql"), str):
                args = parsed
                outcome = Failed(f"{c.name} requires a 'sql' string argument. Try again.")
            else:
                args = parsed
                try:
                    result = await connection.call_tool(c.name, args, active.max_rows)
                except Exception as e:  # a broken tool call must not corrupt or abort the Run
                    trace.add("tool_error", name=c.name, detail=f"{type(_unwrap(e)).__name__}: {_unwrap(e)}")
                    return project(Failed("The tool failed to run. Try again.")), args, None, 1, None
                text = "".join(b.text for b in result.content if isinstance(b, TextContent))
                if result.isError:
                    outcome = Failed(text[:500] or "The tool call was rejected.")
                else:
                    outcome = decode(text)
                if isinstance(outcome, ResultTableMade) and c.name == "run_query":
                    try:
                        # Straight after the call returns, before any other await, so a Run stopped here
                        # still has its table registered (and so listed and swept).
                        self._register_result_table(active, outcome, args)
                    except Exception as e:  # the tool ran; only recording its table failed
                        trace.add("registration_error", name=c.name, detail=f"{type(e).__name__}: {e}")
                        return project(Failed("The query ran but its Result Table could not be recorded. Try again.")), args, None, 1, None
        assert outcome is not None
        content = project(outcome)
        if not is_query:
            return content, args, None, 0, outcome
        executed_sql = getattr(outcome, "executed_sql", None)
        if not isinstance(outcome, Failed | Stopped):
            return content, args, executed_sql, 0, outcome
        # The raw error (or the undecodable text) reaches the Trace, never the LLM.
        raw = outcome.raw_error if outcome.raw_error is not None else getattr(outcome, "raw_content", None)
        await active.queue.put(TraceEntry("query_error", args.get("sql"), raw, outcome.error))
        trace.add("query_error", sql=args.get("sql"), raw_error=raw, sanitized_error=outcome.error)
        return content, args, executed_sql, 1, outcome

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

    def _register_result_table(self, active: _ActiveRun, made: ResultTableMade, args: dict) -> None:
        """Record the Result Table a successful run_query made, against the Cell and Run that made it."""
        title = args.get("title", "")
        self.tables.register_for(
            active.cell_run, made.result_table, title if isinstance(title, str) else "",
            made.columns, active.prompt, made.truncated_to, args.get("sql"), active.max_rows,
        )

    def _prepare_displays(
        self, made: DisplayMade, trace: TraceBuilder,
    ) -> tuple[list[tuple], list[DisplayEvent]]:
        """The Display a tool call made: the definitions [(kind, result_table, definition)] to persist with
        the tool message, and the events to send the user once the step is saved."""
        display = made.display
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

    def _count_display(self, tool: str, outcome: Outcome | None, args: dict) -> CountEvent | None:
        """A successful run_count_query makes a Count Display (shown once it is saved); any other call
        makes none. It has no result table (so no Retention) and lives and dies with its Run."""
        if tool != "run_count_query" or not isinstance(outcome, Counted):
            return None
        counted = outcome
        title = args.get("title")
        return CountEvent(title if isinstance(title, str) else "", counted.count, args["sql"], self.clock())
