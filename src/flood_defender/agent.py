"""The Agent: runs the LLM tool loop for one turn and emits an event stream.

The HTTP/SSE layer is a thin adapter over `Agent.run_turn`. The LLM client is injected.
"""

import asyncio
import json
import sqlite3
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import AsyncIterator, Callable

from mcp import StdioServerParameters

from flood_defender import flagging, queries
from flood_defender.llm import LLMClient, LLMError, TextChunk, ToolCallChunk, UsageChunk
from flood_defender.mcp_connection import McpConnection
from flood_defender.semantic_layer import SemanticLayer
from flood_defender.store import ConversationStore
from flood_defender.trace import TraceBuilder

@dataclass
class AgentConfig:
    """Built from `config.Settings`, which holds every default."""

    telemetry_db: Path
    results_db: Path
    app_db: Path
    semantic_layer: Path
    model: str
    result_ttl_ms: int  # Result Tables expire this long after last Conversation activity
    query_timeout_s: float
    max_result_rows: int
    chart_row_cap: int
    max_failed_attempts: int
    max_llm_steps: int
    clock: Callable[[], int] = lambda: int(time.time() * 1000)  # epoch ms; injectable for tests


class ResultTableExpired(Exception):
    """The Result Table existed but was deleted by expiry (HTTP layer maps this to 410)."""


@dataclass
class Conversation:
    """A Conversation's message history. `id` is None until first persisted by `run_turn`."""

    id: str | None = None
    messages: list[dict] = field(default_factory=list)


@dataclass
class TextDelta:
    text: str


@dataclass
class DisplayEvent:
    kind: str
    result_table: str
    columns: list[str] = field(default_factory=list)
    spec: dict | None = None  # charts: Chart Spec after data-reference resolution
    source_spec: dict | None = None  # charts: Chart Spec as the LLM wrote it (for the Trace)


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
class TurnError:
    """The turn failed partway. `message` was already streamed to the user (and saved); `detail`
    is the technical description (also in the Trace) for harnesses such as the eval runner."""

    message: str
    detail: str


@dataclass
class TurnEnd:
    pass


Event = TextDelta | DisplayEvent | TraceEntry | FlagEvent | TraceReady | TurnError | TurnEnd

GIVE_UP_MESSAGE = (
    "Sorry, I couldn't build a working query for that after several attempts. "
    "Please try rephrasing your question."
)


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
    return _resolve_data(source, table, {"url": f"/api/results/{table}/chart-rows"})


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


class Agent:
    def __init__(self, config: AgentConfig, llm: LLMClient):
        self.config = config
        self.llm = llm
        self.semantic_layer = SemanticLayer(config.semantic_layer)
        self.store = ConversationStore(config.app_db, config.clock)
        self._connection: McpConnection | None = None
        self._restart_lock = asyncio.Lock()
        self._turn_tasks: set[asyncio.Task] = set()
        self.sweep_expired()

    async def start(self) -> None:
        """Launch the long-lived MCP server. Needed before `run_turn`, not for row fetches,
        CSV export or Conversation listing."""
        if self._connection is not None:
            raise RuntimeError("Agent already started")
        connection = McpConnection(self._server_params())
        await connection.start()
        self._connection = connection

    async def _ensure_connection(self) -> McpConnection:
        """The live connection; a dead one (process exited or session broken) is replaced first."""
        async with self._restart_lock:
            connection = self._connection
            if connection is None:
                raise RuntimeError("Agent not started")
            if not connection.alive:
                replacement = McpConnection(self._server_params())
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

    def _server_params(self) -> StdioServerParameters:
        return StdioServerParameters(
            command=sys.executable,
            args=["-m", "flood_defender.mcp_server"],
            # Static settings only. The Conversation id travels in each call's `_meta`, not here
            # and not as a tool argument, so the LLM can't name another Conversation.
            env={
                "TELEMETRY_DB": str(self.config.telemetry_db),
                "RESULTS_DB": str(self.config.results_db),
                "APP_DB": str(self.store.path),
                "CHART_ROW_CAP": str(self.config.chart_row_cap),
                "QUERY_TIMEOUT_S": str(self.config.query_timeout_s),
                "MAX_RESULT_ROWS": str(self.config.max_result_rows),
            },
        )

    def open_conversation(self, conversation_id: str) -> Conversation:
        """Load a saved Conversation (KeyError if unknown)."""
        if not self.store.exists(conversation_id):
            raise KeyError(conversation_id)
        return Conversation(conversation_id, repair_history(self.store.messages(conversation_id)))

    def sweep_expired(self) -> None:
        """Delete Result Tables whose Conversation has been idle past the TTL.

        Runs on app start, before each turn, and before any row read.
        """
        names = [n for n in self.store.due_for_expiry(self.config.result_ttl_ms) if queries.RESULT_TABLE_RE.match(n)]
        if not names:
            return
        if self.config.results_db.exists():
            con = sqlite3.connect(self.config.results_db)
            try:
                for n in names:
                    con.execute(f"DROP TABLE IF EXISTS {n}")
                con.commit()
            finally:
                con.close()
        self.store.mark_expired(names)

    def is_expired(self, result_table: str) -> bool:
        self.sweep_expired()
        return self.store.is_expired(result_table)

    def expired_among(self, result_tables) -> set[str]:
        """Sweep once, then return which of the given Result Tables have expired."""
        self.sweep_expired()
        return self.store.expired_among(result_tables)

    def _raise_if_expired(self, result_table: str) -> None:
        if self.is_expired(result_table):
            raise ResultTableExpired(f"Result Table {result_table} has expired.")

    def fetch_rows(self, result_table: str, columns: list[str] | None = None, **page_args) -> dict:
        """Internal data path used by the row-JSON endpoint. Not an LLM tool.

        Adds per-column display formats from the Semantic Layer."""
        self._raise_if_expired(result_table)
        data = queries.fetch_rows(self.config.results_db, result_table, columns, **page_args)
        data["formats"] = self.semantic_layer.column_formats(data["columns"])
        return data

    def iter_all_rows(self, result_table: str):
        """Internal data path for CSV export of the full Result Table. Not an LLM tool."""
        self._raise_if_expired(result_table)
        return queries.iter_all_rows(self.config.results_db, result_table)

    def fetch_chart_rows(self, result_table: str) -> list[dict]:
        """Internal data path for the chart-rows endpoint (chart cap). Not an LLM tool."""
        self._raise_if_expired(result_table)
        return queries.fetch_records(self.config.results_db, result_table, self.config.chart_row_cap)

    async def run_turn(self, conversation: Conversation, user_message: str) -> AsyncIterator[Event]:
        """Run one turn on the Agent's MCP connection (`start()` first).

        The turn runs in a producer task. If the consumer stops early (client disconnect), the
        producer is cancelled and awaited; the MCP server keeps running."""
        if self._connection is None:
            raise RuntimeError("Agent not started: call `await agent.start()` or use `async with agent`")
        queue: asyncio.Queue = asyncio.Queue()
        task = asyncio.create_task(self._produce(conversation, user_message, queue))
        self._turn_tasks.add(task)
        task.add_done_callback(self._turn_tasks.discard)
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

    async def _produce(self, conversation: Conversation, user_message: str, queue: asyncio.Queue):
        try:
            await self._run(conversation, user_message, queue)
            await queue.put(TurnEnd())
        except asyncio.CancelledError:
            raise
        except BaseException as e:  # surfaced to the consumer
            await queue.put(e)

    async def _run(self, conversation: Conversation, user_message: str, queue: asyncio.Queue):
        self.sweep_expired()
        if conversation.id is None:
            conversation.id = self.store.create_conversation(user_message[:60])
        turn_id = self.store.start_turn(conversation.id, user_message)
        self._record(conversation, turn_id, {"role": "user", "content": user_message})
        trace = TraceBuilder(self.config.model)
        try:
            await self._converse(conversation, turn_id, user_message, trace, queue)
        except asyncio.CancelledError:
            self.store.save_trace(turn_id, trace.finish())
            raise
        except Exception as e:
            await self._fail_turn(conversation, turn_id, trace, queue, e)
        finished = trace.finish()
        self.store.save_trace(turn_id, finished)
        await queue.put(TraceReady(finished))

    async def _fail_turn(self, conversation, turn_id, trace: TraceBuilder, queue, error: Exception) -> None:
        """A turn failed partway: tell the user, record it in the Trace and in the history."""
        error = _unwrap(error)
        detail = f"{type(error).__name__}: {error}"
        if isinstance(error, LLMError):
            message = f"Sorry, I couldn't get a complete answer from the model. {error}"
        else:
            message = f"Sorry, something went wrong while handling that request ({type(error).__name__}). Please try again."
        trace.add("error", message=message, detail=detail)
        self._record(conversation, turn_id, {"role": "assistant", "content": message})
        await queue.put(TextDelta(message))
        await queue.put(TurnError(message, detail))

    async def _converse(self, conversation, turn_id, user_message, trace: TraceBuilder, queue):
        connection = await self._ensure_connection()
        tools = connection.tools
        failed_attempts = 0
        for _ in range(self.config.max_llm_steps):
            system_prompt = self.semantic_layer.system_prompt(self.store.live_result_tables(conversation.id))
            messages = [{"role": "system", "content": system_prompt}]
            messages += conversation.messages
            text, calls, usage = "", [], None
            step_started = time.monotonic()
            async for chunk in self.llm.stream_chat(messages, tools):
                if isinstance(chunk, TextChunk):
                    text += chunk.text
                    await queue.put(TextDelta(chunk.text))
                elif isinstance(chunk, ToolCallChunk):
                    calls.append(chunk)
                elif isinstance(chunk, UsageChunk):
                    usage = chunk
            trace.llm_step(system_prompt, text, calls, usage, step_started)
            await self._flag_unseen_values(text, system_prompt, messages, trace, queue)
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
            for c in calls:
                call_started = time.monotonic()
                content, args, executed_sql, failed = await self._run_tool(connection, c, queue, trace, conversation.id)
                failed_attempts += failed
                trace.tool_call(c.name, args, content, call_started, executed_sql)
                self._trace_result_table(trace, content)
                displays = await self._prepare_displays(
                    conversation, content, args, queue, user_message, trace
                )
                items.append(({"role": "tool", "tool_call_id": c.id, "content": content}, displays))
            self.store.add_step(conversation.id, turn_id, items)
            conversation.messages.extend(m for m, _ in items)
            if not calls:
                break
            if failed_attempts >= self.config.max_failed_attempts:
                await self._end_with_notice(conversation, turn_id, GIVE_UP_MESSAGE, queue)
                break
        else:
            trace.add("step_limit", max_steps=self.config.max_llm_steps)
            await self._end_with_notice(conversation, turn_id, STEP_LIMIT_MESSAGE, queue)

    async def _end_with_notice(self, conversation, turn_id, message: str, queue) -> None:
        self._record(conversation, turn_id, {"role": "assistant", "content": message})
        await queue.put(TextDelta(message))

    async def _run_tool(
        self, connection: McpConnection, c: ToolCallChunk, queue: asyncio.Queue, trace: TraceBuilder, conversation_id: str
    ) -> tuple[str, dict, str | None, int]:
        """Execute one LLM tool call. Returns (content, args, executed_sql, failed 0/1).

        Never raises for problems the LLM caused (bad JSON, missing `sql`) or tool failures: it
        returns an error result the LLM can act on, and a failed run_query counts toward the limit."""
        is_query = c.name == "run_query"
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
                content = _fail("run_query requires a 'sql' string argument. Try again.")
            else:
                args = parsed
                try:
                    result = await connection.call_tool(c.name, args, conversation_id)
                    content = "".join(b.text for b in result.content if hasattr(b, "text"))
                    if result.isError:
                        content = _fail(content[:500] or "The tool call was rejected.")
                except Exception as e:  # a broken tool call must not corrupt or abort the turn
                    trace.add("tool_error", name=c.name, detail=f"{type(_unwrap(e)).__name__}: {_unwrap(e)}")
                    content = _fail("The tool failed to run. Try again.")
                    return content, args, None, 1
        if is_query:
            content, executed_sql = self._strip_executed_sql(content)
            content, failed = await self._split_query_error(content, args, queue, trace)
            return content, args, executed_sql, failed
        return content, args, None, 0

    @staticmethod
    async def _flag_unseen_values(text: str, system_prompt: str, messages: list[dict], trace, queue) -> None:
        """Flag IP-like / large-number tokens in a completed reply that the LLM was never sent.

        Allowed context = what this step's LLM call received, except the LLM's own earlier prose
        (so repeating an invented value is flagged again): system prompt (Semantic Layer, Result
        Tables), user messages, tool results (Query Metadata), and tool-call SQL arguments."""
        if not text:
            return
        allowed = [system_prompt]
        for m in messages:
            if m["role"] in ("user", "tool"):
                allowed.append(m["content"] or "")
            elif m["role"] == "assistant":
                allowed += [c["function"]["arguments"] for c in m.get("tool_calls") or []]
        found = flagging.find_unseen(text, allowed)
        if not found:
            return
        items = [{"type": t, "token": tok} for t, tok in found]
        for item in items:
            trace.add("flag", **item)
        await queue.put(FlagEvent([i["token"] for i in items], items))

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
        """Strip the raw SQLite error from run_query output before the LLM sees it, and
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

    def _record(self, conversation: Conversation, turn_id: int, message: dict) -> int:
        conversation.messages.append(message)
        return self.store.add_message(conversation.id, turn_id, message)

    async def _prepare_displays(
        self, conversation: Conversation, tool_content: str, args: dict, queue: asyncio.Queue,
        request: str, trace: TraceBuilder,
    ) -> list[tuple]:
        """Register a new Result Table, emit any Display to the user, and return the Display
        definitions [(kind, result_table, definition)] to persist with the tool message."""
        try:
            payload = json.loads(tool_content)
        except ValueError:
            return []
        if not isinstance(payload, dict):
            return []
        if payload.get("success") and payload.get("result_table") and "display" not in payload:
            title = args.get("title", "")
            self.store.add_result_table(
                conversation.id, payload["result_table"], title if isinstance(title, str) else "",
                payload.get("columns", []), request,
            )
        display = payload.get("display")
        if not display:
            return []
        table = display["result_table"]
        owned = self.store.owns_result_table(conversation.id, table)
        if display["kind"] == "chart":
            source = display["spec"]
            resolved = resolve_chart_data(source, table)
            trace.add("chart_spec", result_table=table, source_spec=source, spec=resolved)
            await queue.put(DisplayEvent("chart", table, spec=resolved, source_spec=source))
            return [("chart", table, {"spec": source})] if owned else []
        if not owned:
            return []
        await queue.put(DisplayEvent(display["kind"], table, display["columns"]))
        return [(display["kind"], table, {"columns": display["columns"]})]
