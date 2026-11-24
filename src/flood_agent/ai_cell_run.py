"""AI Cell Runs: everything an AI Cell's Run does, from starting the Run down.

A Run sends the LLM the system prompt, the AI Cell context above the Cell and its prompt, then runs the LLM step
loop: each step's text is streamed, its tool calls are run on the MCP server, and their outcomes become Result
Tables, Displays and Count Displays. Values in a reply the LLM was never sent are flagged. The loop ends with an
answer, a give-up or step-limit notice, the user's stop, or a failure.

It records the Run in the NotebookStore (its messages, Displays and Trace) and its Result Tables in the Table
Catalog against the Cell and Run.
"""

import asyncio
import json
import time
from dataclasses import dataclass
from typing import AsyncIterator, Awaitable, Callable

from mcp.types import TextContent

from flood_agent import ai_cell_context, flagging
from flood_agent.catalog import Catalog, CellRun, Table
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
from flood_agent.notebooks import COUNT_KIND, DEFAULT_MAX_ROWS, NotebookStore
from flood_agent.semantic_layer import SemanticLayer
from flood_agent.tool_outcome import Counted, DisplayMade, Failed, Outcome, ResultTableMade, Stopped, decode, project
from flood_agent.trace import TraceBuilder, context_tokens

ClientFactory = Callable[[str], LLMClient]
"""Gives the client that runs a model id: the real OpenRouter client (made lazily, reused) or a scripted fake."""

ConnectionProvider = Callable[[], Awaitable[McpConnection]]
"""Gives the live MCP connection, restarting a dead server first (the Agent's, ADR 0002)."""


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


UNFINISHED = "The Run did not finish."  # a failed Run's reason when its task ended without closing it

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
    """Runs AI Cells and stops them. Built once by the Agent; the Agent keeps the rule that one Cell runs per
    Notebook at a time, and supervises the MCP connection this gets from `connection`."""

    def __init__(
        self, notebooks: NotebookStore, tables: Catalog, semantic_layer: SemanticLayer, client_for: ClientFactory,
        connection: ConnectionProvider, default_model: str, spec_of: Callable[[str], ModelSpec | None],
        max_llm_steps: int, max_failed_attempts: int, clock: Callable[[], int],
    ):
        self.notebooks, self.tables, self.semantic_layer = notebooks, tables, semantic_layer
        self.client_for, self._connection = client_for, connection
        self.default_model, self.spec_of = default_model, spec_of
        self.max_llm_steps, self.max_failed_attempts, self.clock = max_llm_steps, max_failed_attempts, clock
        self.tasks: set[asyncio.Task] = set()  # every running Run's producer task
        self._keyed: dict[str, asyncio.Task] = {}  # running Runs by cell id
        self._stopped: set[asyncio.Task] = set()  # Runs `stop` cancelled

    async def run(self, cell: dict) -> AsyncIterator[Event]:
        """Run an AI Cell with what its Draft holds and stream its events.

        It is sent the system prompt, then the AI Cells above it in order (each as its latest Run stored it:
        prompt, answer, tool calls and results), then its own prompt; cells below are never sent. Its Run
        replaces the cell's earlier one. If the stream is abandoned (the client left), the Run is cancelled
        and waited for."""
        task, queue, release = self._start(cell)
        try:
            async for item in self._consume(task, queue):
                yield item
        finally:
            release()  # now, not whenever the task's done callback gets its turn

    def stop(self, cell_id: str) -> bool:
        """Stop a running AI Cell now. What it had streamed is kept (history, Trace) and its events end
        normally. False if the Cell is not running here."""
        task = self._keyed.get(cell_id)
        if task is None or task.done():
            return False
        self._stopped.add(task)
        task.cancel()
        return True

    async def aclose(self) -> None:
        """Cancel every running Run and wait for it, so none is left using the MCP connection."""
        tasks = list(self.tasks)
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    def _start(self, cell: dict) -> tuple[asyncio.Task, _TurnQueue, Callable[[], None]]:
        notebook_id, cell_id, draft = cell["notebook_id"], cell["id"], cell["draft"]
        context = ai_cell_context.context_above(self.notebooks.cells(notebook_id), cell_id)
        history, above, fingerprint = context.messages, context.above, context.fingerprint
        settings = {"model": draft.get("model") or self.default_model, "effort": draft.get("effort"),
                    "max_rows": draft.get("max_rows") or DEFAULT_MAX_ROWS}
        run_id = self.notebooks.start_run(cell_id, draft["prompt"], settings, fingerprint)
        run = CellRun(cell_id, run_id)
        session_id = f"notebook-{notebook_id}"  # lets OpenRouter route every step of every cell to one cache

        def prompt_tables() -> list[Table]:
            return ai_cell_context.listed_result_tables(above, cell_id, run_id, self.tables.tables_of_run)

        queue = _TurnQueue()
        task = asyncio.create_task(self._produce(history, prompt_tables, run, session_id, draft["prompt"], queue, settings))
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)
        self._keyed[cell_id] = task

        def release(_t: asyncio.Task | None = None) -> None:
            """The cell is no longer running: close a Run the task could not."""
            if self._keyed.get(cell_id) is task:
                del self._keyed[cell_id]
            if task.done() and self.notebooks.status_of(run_id) == "running":  # cancelled or crashed first
                if task in self._stopped:
                    self.notebooks.finish_run(run_id, "stopped")
                else:
                    self.notebooks.finish_run(run_id, "failed", UNFINISHED)
        task.add_done_callback(release)
        return task, queue, release

    async def _produce(
        self, history: list[dict], prompt_tables: Callable[[], list[Table]], run: CellRun, session_id: str,
        prompt: str, queue: _TurnQueue, settings: dict,
    ):
        def end(trace: dict, status: str, error: str | None) -> TraceReady:
            self.notebooks.finish_run(run.run_id, status, error)
            return TraceReady(trace)

        try:
            self._record(history, run, {"role": "user", "content": prompt})
            await self._exchange(history, prompt_tables, run, session_id, prompt, queue, settings["max_rows"],
                                 settings["model"], settings["effort"], end=end)
            await queue.put(TurnEnd())
        except asyncio.CancelledError:
            raise
        except BaseException as e:  # surfaced to the consumer
            await queue.put(e)

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

    async def _exchange(
        self, history: list[dict], prompt_tables: Callable[[], list[Table]], run: CellRun, session_id: str,
        request: str, queue: _TurnQueue, max_result_rows: int, model: str, effort: str | None,
        end: Callable[[dict, str, str | None], object],
    ):
        """The one path an LLM exchange runs through: `history` (already ending in the user's message, extended
        in place), and `prompt_tables` (the Result Tables the system prompt lists, asked before every step). Its
        messages, Displays and Trace are stored with `run`, and its Result Tables recorded against it.

        `end` gets the finished Trace and how the loop ended: ok (an answer, including a give-up or step-limit
        notice), stopped (the user's stop), or failed with the value-free message the user was shown."""
        trace = TraceBuilder(model, on_add=lambda entry: queue.put_nowait(TraceLive(entry)))
        live = _LiveStep()
        status, error = "ok", None
        try:
            await self._converse(history, prompt_tables, run, session_id, request, trace, queue, max_result_rows, model, effort, live)
        except asyncio.CancelledError:
            if asyncio.current_task() not in self._stopped:  # the client left or the app is closing
                self._save_trace(run, trace.finish())
                raise
            self._record_stop(history, run, live, trace, queue)
            status = "stopped"
        except Exception as e:
            status, error = "failed", await self._fail_turn(history, run, trace, queue, e)
        finished = trace.finish()
        self._save_trace(run, finished)
        await queue.put(end(finished, status, error))

    def _record(self, history: list[dict], run: CellRun, message: dict) -> None:
        history.append(message)
        self.notebooks.add_message(run.run_id, message)

    def _save_trace(self, run: CellRun, trace: dict) -> None:
        self.notebooks.save_trace(run.run_id, trace, context_tokens(trace))

    def _record_stop(self, history, run: CellRun, live: _LiveStep, trace: TraceBuilder, queue: _TurnQueue) -> None:
        """The user stopped the turn: keep the text of the step in progress, and say so, in the page,
        the history and the Trace. A step cut off before its tool calls were answered keeps only its text."""
        partial = live.text if live.open else ""
        if live.open and not live.in_trace:
            trace.llm_step(live.system_prompt, live.text, [], None, live.started)
        trace.add("stopped")
        message = (partial.rstrip() + "\n\n" if partial.strip() else "") + ai_cell_context.STOPPED_NOTE
        self._record(history, run, {"role": "assistant", "content": message})
        queue.put_nowait(TextDelta(queue.paragraph_break() + ai_cell_context.STOPPED_NOTE))

    async def _fail_turn(self, history, run: CellRun, trace: TraceBuilder, queue: _TurnQueue, error: BaseException) -> str:
        """A turn failed partway: tell the user, record it in the Trace and in the history. Returns the
        value-free message the user was shown."""
        error = _unwrap(error)
        detail = f"{type(error).__name__}: {error}"
        if isinstance(error, LLMError):
            message = f"Sorry, I couldn't get a complete answer from the model. {error}"
        else:
            message = f"Sorry, something went wrong while handling that request ({type(error).__name__}). Please try again."
        trace.add("error", message=message, detail=detail)
        self._record(history, run, {"role": "assistant", "content": message})
        await queue.put_notice(message)
        await queue.put(TurnError(message, detail))
        return message

    async def _converse(
        self, history: list[dict], prompt_tables: Callable[[], list[Table]], run: CellRun, session_id: str,
        user_message: str,
        trace: TraceBuilder, queue: _TurnQueue, max_result_rows: int, model: str, effort: str | None = None, live: _LiveStep | None = None,
    ):
        live = live or _LiveStep()
        llm = self.client_for(model)
        spec = self.spec_of(model)
        # Only a non-default Effort is sent; the default sends nothing extra.
        reasoning = {"effort": effort} if effort and spec and effort != spec.default_effort else None
        connection = await self._connection()
        tools = connection.tools
        failed_attempts = 0
        for call_number in range(1, self.max_llm_steps + 1):
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
                content, args, executed_sql, failed, outcome = await self._run_tool(
                    connection, c, queue, trace, run, user_message, max_result_rows
                )
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
            _, display_ids = self.notebooks.add_step(run.run_id, items)
            for event, index in step_displays:
                if isinstance(event, CountEvent):
                    event.display_id = display_ids[index][-1]  # a call makes one Display: its count is the last
                await queue.put(event)
            history.extend(m for m, _ in items)
            live.open = False
            if not calls:
                break
            if failed_attempts >= self.max_failed_attempts:
                await self._end_with_notice(history, run, GIVE_UP_MESSAGE, queue)
                break
        else:
            trace.add("step_limit", max_steps=self.max_llm_steps)
            await self._end_with_notice(history, run, STEP_LIMIT_MESSAGE, queue)

    async def _end_with_notice(self, history: list[dict], run: CellRun, message: str, queue: _TurnQueue) -> None:
        self._record(history, run, {"role": "assistant", "content": message})
        await queue.put_notice(message)

    async def _run_tool(
        self, connection: McpConnection, c: ToolCallChunk, queue: asyncio.Queue, trace: TraceBuilder,
        run: CellRun, request: str, max_result_rows: int,
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
                    result = await connection.call_tool(c.name, args, max_result_rows)
                except Exception as e:  # a broken tool call must not corrupt or abort the turn
                    trace.add("tool_error", name=c.name, detail=f"{type(_unwrap(e)).__name__}: {_unwrap(e)}")
                    return project(Failed("The tool failed to run. Try again.")), args, None, 1, None
                text = "".join(b.text for b in result.content if isinstance(b, TextContent))
                if result.isError:
                    outcome = Failed(text[:500] or "The tool call was rejected.")
                else:
                    outcome = decode(text)
                if isinstance(outcome, ResultTableMade) and c.name == "run_query":
                    try:
                        # Straight after the call returns, before any other await, so a turn stopped here
                        # still has its table registered (and so listed and swept).
                        self._register_result_table(run, outcome, args, request, max_result_rows)
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
        await queue.put(TraceEntry("query_error", args.get("sql"), raw, outcome.error))
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

    def _register_result_table(
        self, run: CellRun, made: ResultTableMade, args: dict, request: str, max_result_rows: int
    ) -> None:
        """Record the Result Table a successful run_query made, against the Cell and Run that made it."""
        title = args.get("title", "")
        self.tables.register_for(
            run, made.result_table, title if isinstance(title, str) else "",
            made.columns, request, made.truncated_to, args.get("sql"), max_result_rows,
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
