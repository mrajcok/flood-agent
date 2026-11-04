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
from mcp.types import TextContent

from flood_agent import flagging, lookup_tables, queries, table_names
from flood_agent.llm import LLMClient, LLMError, TextChunk, ToolCallChunk, UsageChunk
from flood_agent.mcp_connection import McpConnection, Transport, stdio_transport
from flood_agent.mcp_protocol import QUERY_TOOLS
from flood_agent.mcp_server import ServerSettings
from flood_agent.semantic_layer import SemanticLayer
from flood_agent.store import ConversationStore, TablesChoice
from flood_agent.trace import TraceBuilder


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


DAY_MS = 24 * 3600 * 1000


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
    turn_id: int | None = None


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
        PSV export or Conversation listing."""
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
        # Static settings only. The Conversation id travels in each call's `_meta`, not here
        # and not as a tool argument, so the LLM can't name another Conversation.
        return ServerSettings(
            telemetry_db=self.config.telemetry_db,
            results_db=self.config.results_db,
            app_db=self.store.path,
            query_timeout_s=self.config.query_timeout_s,
            chart_row_cap=self.config.chart_row_cap,
        )

    def _server_params(self) -> StdioServerParameters:
        return StdioServerParameters(
            command=sys.executable, args=["-m", "flood_agent.mcp_server"], env=self._server_settings().to_env()
        )

    def _transport(self) -> Transport:
        """How a new connection reaches its server: a stdio subprocess (tests swap in an in-process one)."""
        return stdio_transport(self._server_params())

    def open_conversation(self, conversation_id: str) -> Conversation:
        """Load a saved Conversation (KeyError if unknown)."""
        if not self.store.exists(conversation_id):
            raise KeyError(conversation_id)
        return Conversation(conversation_id, repair_history(self.store.messages(conversation_id)))

    def delete_conversation(self, conversation_id: str, tables: TablesChoice = "keep") -> None:
        """Delete a saved Conversation; `tables` (keep, delete_rt, delete_all) says which of its
        live Result Tables to drop (KeyError if unknown)."""
        if not self.store.exists(conversation_id):
            raise KeyError(conversation_id)
        self._drop_result_tables(self.store.delete_conversation(conversation_id, tables))

    def delete_turn(self, conversation_id: str, turn_id: int, tables: TablesChoice = "keep") -> None:
        """Delete one prompt/answer exchange; `tables` says which Result Tables it created to drop
        (KeyError if unknown)."""
        self._drop_result_tables(self.store.delete_turn(conversation_id, turn_id, tables))

    def _drop_result_tables(self, names: list[str]) -> None:
        if not names or not self.config.results_db.exists():
            return
        con = sqlite3.connect(self.config.results_db)
        try:
            for n in names:
                con.execute(f"DROP TABLE IF EXISTS {table_names.quote(n)}")
            con.commit()
        finally:
            con.close()

    def sweep_expired(self) -> None:
        """Delete Result Tables whose Retention has passed since their creation.

        Runs on app start, before each turn, and before any row read.
        """
        names = self.store.due_for_expiry(self.config.result_ttl_ms)
        if not names:
            return
        self._drop_result_tables(names)
        self.store.mark_expired(names)

    def is_expired(self, result_table: str) -> bool:
        self.sweep_expired()
        return self.store.is_expired(result_table)

    def expired_among(self, result_tables) -> set[str]:
        """Sweep once, then return which of the given Result Tables have expired."""
        self.sweep_expired()
        return self.store.expired_among(result_tables)

    def _is_live_result_table(self, result_table: str) -> bool:
        """A registered Result Table that has not expired (after sweeping)."""
        return self.store.has_result_table(result_table) and not self.is_expired(result_table)

    def _require_live_result_table(self, result_table: str) -> None:
        """KeyError unless the name is a registered Result Table; ResultTableExpired if it has expired."""
        if not self.store.has_result_table(result_table):
            raise KeyError(result_table)
        if not self._is_live_result_table(result_table):
            raise ResultTableExpired(f"Result Table {result_table} has expired.")

    def add_lookup_table(self, suffix: str, text: str, overwrite: bool = False) -> dict:
        """Create `lookup_<suffix>` from pasted rows and register it. Touches no Conversation, message
        or Trace, and the LLM is told nothing. Raises `InvalidLookupTable` for a bad name or paste."""
        name = lookup_tables.table_name(suffix)
        parsed = lookup_tables.parse_rows(text)
        registered = self.store.is_lookup_table(name)
        try:
            lookup_tables.write_table(self.config.results_db, name, parsed, replace=registered and overwrite)
        except lookup_tables.TableExists:
            raise lookup_tables.InvalidLookupTable(
                f"A table named {name} already exists. Tick Overwrite existing table to replace it."
                if registered else f"A table named {name} already exists and is not a Lookup Table."
            )
        # The registry is in the app database, a separate file, so it cannot join the results-database
        # transaction. A crash between the two leaves a table the registry does not describe (its row
        # count or columns stale, or a new table unregistered and so unreadable) until it is added again.
        self.store.register_lookup_table(name, parsed.columns, len(parsed.rows))
        return {"name": name, "row_count": len(parsed.rows), "columns": parsed.columns}

    def list_tables(self) -> dict[str, list[str]]:
        """Names (only) of every table the user can query, by kind, each sorted: Telemetry Database
        tables described in the Semantic Layer, Lookup Tables, and live Result Tables from every
        Conversation. For the web page only; touches no Conversation, message or Trace."""
        self.sweep_expired()
        return {
            "telemetry": sorted(self.semantic_layer.table_names()),
            "lookup": sorted(t["name"] for t in self.store.lookup_tables()),
            "result": self.store.all_live_result_table_names(),
        }

    def result_table_settings(self, result_table: str) -> dict:
        """The Result Table's name and retention in days (KeyError if unknown)."""
        found = self.store.result_table_settings(result_table, self.config.result_ttl_ms)
        if found is None:
            raise KeyError(result_table)
        return {"name": found["name"], "retention_days": found["retention_ms"] / DAY_MS}

    def update_result_table(
        self, result_table: str, name: str | None = None, retention_days: float | None = None
    ) -> dict:
        """Rename (a real SQL rename) and/or change retention of a live Result Table.

        A `name` equal to the current one changes nothing. Returns the new settings. ValueError,
        changing nothing, if the new name is not allowed. Retention is unchanged by a rename."""
        self._require_live_result_table(result_table)
        if name is not None and name != result_table:
            self._rename(result_table, name)
            result_table = name
        if retention_days is not None:
            self.store.update_result_table(result_table, max(1, round(retention_days * DAY_MS)))
        return self.result_table_settings(result_table)

    def _rename(self, old: str, new: str) -> None:
        """Rename in the results database, then in the app database. These are two files, so if the
        second step fails the first is undone."""
        problem = queries.check_new_table_name(new)
        if problem:
            raise ValueError(problem)
        queries.rename_table(self.config.results_db, old, new)  # ValueError if `new` is taken
        try:
            self.store.rename_result_table(old, new)
        except sqlite3.IntegrityError:  # `new` is the name of an expired table still in the registry
            queries.rename_table(self.config.results_db, new, old)
            raise ValueError(f"A table named {new} already exists.")
        except BaseException:
            queries.rename_table(self.config.results_db, new, old)
            raise

    def fetch_rows(self, result_table: str, columns: list[str] | None = None, **page_args) -> dict:
        """Internal data path used by the row-JSON endpoint. Not an LLM tool.

        Adds per-column display formats from the Semantic Layer."""
        self._require_live_result_table(result_table)
        data = queries.fetch_rows(self.config.results_db, result_table, columns, **page_args)
        data["formats"] = self.semantic_layer.column_formats(data["columns"])
        data["truncated_to"] = self.store.truncated_to(result_table)
        try:
            data.update(self.result_table_settings(result_table))
        except KeyError:  # not owned by a Conversation: no user settings
            data.update(name=result_table, retention_days=self.config.result_ttl_ms / DAY_MS)
        return data

    def iter_all_rows(self, result_table: str):
        """Internal data path for PSV export of the full Result Table. Not an LLM tool."""
        self._require_live_result_table(result_table)
        return queries.iter_all_rows(self.config.results_db, result_table)

    def fetch_chart_rows(self, result_table: str) -> list[dict]:
        """Internal data path for the chart-rows endpoint (chart cap). Not an LLM tool."""
        self._require_live_result_table(result_table)
        return queries.fetch_records(self.config.results_db, result_table, self.config.chart_row_cap)

    async def run_turn(
        self, conversation: Conversation, user_message: str, max_result_rows: int
    ) -> AsyncIterator[Event]:
        """Run one turn on the Agent's MCP connection (`start()` first).

        The turn runs in a producer task. If the consumer stops early (client disconnect), the
        producer is cancelled and awaited; the MCP server keeps running.

        `max_result_rows` is the row cap for this turn's queries; the user chooses it in the UI."""
        if self._connection is None:
            raise RuntimeError("Agent not started: call `await agent.start()` or use `async with agent`")
        queue: asyncio.Queue = asyncio.Queue()
        task = asyncio.create_task(self._produce(conversation, user_message, queue, max_result_rows))
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

    async def _produce(
        self, conversation: Conversation, user_message: str, queue: asyncio.Queue, max_result_rows: int
    ):
        try:
            await self._run(conversation, user_message, queue, max_result_rows)
            await queue.put(TurnEnd())
        except asyncio.CancelledError:
            raise
        except BaseException as e:  # surfaced to the consumer
            await queue.put(e)

    async def _run(
        self, conversation: Conversation, user_message: str, queue: asyncio.Queue, max_result_rows: int
    ):
        self.sweep_expired()
        if conversation.id is None:
            conversation.id = self.store.create_conversation()
        turn_id = self.store.start_turn(conversation.id, user_message)
        self._record(conversation, turn_id, {"role": "user", "content": user_message})
        trace = TraceBuilder(self.config.model)
        try:
            await self._converse(conversation, turn_id, user_message, trace, queue, max_result_rows)
        except asyncio.CancelledError:
            self.store.save_trace(turn_id, trace.finish())
            raise
        except Exception as e:
            await self._fail_turn(conversation, turn_id, trace, queue, e)
        finished = trace.finish()
        self.store.save_trace(turn_id, finished)
        await queue.put(TraceReady(finished, turn_id))

    async def _fail_turn(self, conversation, turn_id, trace: TraceBuilder, queue, error: BaseException) -> None:
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

    async def _converse(
        self, conversation, turn_id, user_message, trace: TraceBuilder, queue, max_result_rows: int
    ):
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
                content, args, executed_sql, failed = await self._run_tool(
                    connection, c, queue, trace, conversation.id, max_result_rows
                )
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
        self, connection: McpConnection, c: ToolCallChunk, queue: asyncio.Queue, trace: TraceBuilder,
        conversation_id: str, max_result_rows: int,
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
                    result = await connection.call_tool(c.name, args, conversation_id, max_result_rows)
                    content = "".join(b.text for b in result.content if isinstance(b, TextContent))
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

    def _record(self, conversation: Conversation, turn_id: int, message: dict) -> int:
        assert conversation.id is not None  # run_turn persists the Conversation first
        conversation.messages.append(message)
        return self.store.add_message(conversation.id, turn_id, message)

    async def _prepare_displays(
        self, conversation: Conversation, tool_content: str, args: dict, queue: asyncio.Queue,
        request: str, trace: TraceBuilder,
    ) -> list[tuple]:
        """Register a new Result Table, emit any Display to the user, and return the Display
        definitions [(kind, result_table, definition)] to persist with the tool message."""
        assert conversation.id is not None  # run_turn persists the Conversation first
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
                payload.get("columns", []), request, payload.get("truncated_to"),
            )
        display = payload.get("display")
        if not display:
            return []
        table = display["result_table"]
        # Any live Result Table may be shown (ADR 0003); the Display is saved with this
        # Conversation, but the table stays owned by the one that created it.
        live = self._is_live_result_table(table)
        if display["kind"] == "chart":
            source = display["spec"]
            resolved = resolve_chart_data(source, table)
            trace.add("chart_spec", result_table=table, source_spec=source, spec=resolved)
            await queue.put(DisplayEvent("chart", table, spec=resolved, source_spec=source))
            return [("chart", table, {"spec": source})] if live else []
        if not live:
            return []
        await queue.put(DisplayEvent(display["kind"], table, display["columns"]))
        return [(display["kind"], table, {"columns": display["columns"]})]
