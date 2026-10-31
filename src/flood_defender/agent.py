"""The Agent: runs the LLM tool loop for one turn and emits an event stream.

The HTTP/SSE layer is a thin adapter over `Agent.run_turn`. The LLM client is injected.
"""

import asyncio
import json
import sqlite3
import sys
import time
from contextlib import AsyncExitStack
from dataclasses import dataclass, field
from pathlib import Path
from typing import AsyncIterator, Callable

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from flood_defender import flagging, queries
from flood_defender.llm import LLMClient, TextChunk, ToolCallChunk, UsageChunk
from flood_defender.semantic_layer import SemanticLayer
from flood_defender.store import ConversationStore
from flood_defender.trace import TraceBuilder

MAX_LLM_STEPS = 10


@dataclass
class AgentConfig:
    telemetry_db: Path
    results_db: Path
    semantic_layer: Path
    model: str
    query_timeout_s: float = 30.0
    max_result_rows: int = 1_000_000
    max_failed_attempts: int = 3
    app_db: Path | None = None  # defaults to app.db beside the results database
    result_ttl_ms: int = 24 * 3600 * 1000  # Result Tables expire this long after last Conversation activity
    clock: Callable[[], int] = lambda: int(time.time() * 1000)  # epoch ms; injectable for tests
    chart_row_cap: int = 10_000


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
class TurnEnd:
    pass


Event = TextDelta | DisplayEvent | TraceEntry | FlagEvent | TraceReady | TurnEnd

GIVE_UP_MESSAGE = (
    "Sorry, I couldn't build a working query for that after several attempts. "
    "Please try rephrasing your question."
)


def _openai_tool(t) -> dict:
    return {
        "type": "function",
        "function": {"name": t.name, "description": t.description or "", "parameters": t.inputSchema},
    }


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
        self.store = ConversationStore(config.app_db or config.results_db.parent / "app.db", config.clock)
        self.sweep_expired()

    def open_conversation(self, conversation_id: str) -> Conversation:
        """Load a saved Conversation (KeyError if unknown)."""
        if not self.store.exists(conversation_id):
            raise KeyError(conversation_id)
        return Conversation(conversation_id, self.store.messages(conversation_id))

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
        """Run one turn. The MCP session lives entirely inside a single producer task."""
        queue: asyncio.Queue = asyncio.Queue()
        task = asyncio.create_task(self._produce(conversation, user_message, queue))
        try:
            while True:
                item = await queue.get()
                if isinstance(item, BaseException):
                    raise item
                yield item
                if isinstance(item, TurnEnd):
                    break
        finally:
            await task if task.done() else task.cancel()

    async def _produce(self, conversation: Conversation, user_message: str, queue: asyncio.Queue):
        try:
            await self._run(conversation, user_message, queue)
        except BaseException as e:  # surfaced to the consumer
            await queue.put(e)

    async def _run(self, conversation: Conversation, user_message: str, queue: asyncio.Queue):
        self.sweep_expired()
        if conversation.id is None:
            conversation.id = self.store.create_conversation(user_message[:60])
        turn_id = self.store.start_turn(conversation.id, user_message)
        params = StdioServerParameters(
            command=sys.executable,
            args=["-m", "flood_defender.mcp_server"],
            env={
                "TELEMETRY_DB": str(self.config.telemetry_db),
                "RESULTS_DB": str(self.config.results_db),
                "APP_DB": str(self.store.path),
                "CONVERSATION_ID": conversation.id,
                "CHART_ROW_CAP": str(self.config.chart_row_cap),
                "QUERY_TIMEOUT_S": str(self.config.query_timeout_s),
                "MAX_RESULT_ROWS": str(self.config.max_result_rows),
            },
        )
        async with AsyncExitStack() as stack:
            read, write = await stack.enter_async_context(stdio_client(params))
            session = await stack.enter_async_context(ClientSession(read, write))
            await session.initialize()
            tools = [_openai_tool(t) for t in (await session.list_tools()).tools]

            self._record(conversation, turn_id, {"role": "user", "content": user_message})
            failed_attempts = 0
            trace = TraceBuilder(self.config.model)
            for _ in range(MAX_LLM_STEPS):
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
                self._record(conversation, turn_id, assistant)
                if not calls:
                    break
                for c in calls:
                    args = json.loads(c.arguments or "{}")
                    call_started = time.monotonic()
                    result = await session.call_tool(c.name, args)
                    content = "".join(b.text for b in result.content if hasattr(b, "text"))
                    executed_sql = None
                    if c.name == "run_query":
                        content, executed_sql = self._strip_executed_sql(content)
                        content, failed = await self._split_query_error(content, args, queue, trace)
                        failed_attempts += failed
                    message_id = self._record(
                        conversation, turn_id, {"role": "tool", "tool_call_id": c.id, "content": content}
                    )
                    trace.tool_call(c.name, args, content, call_started, executed_sql)
                    self._trace_result_table(trace, content)
                    await self._emit_display(
                        conversation, turn_id, message_id, content, queue, c.arguments, user_message, trace
                    )
                if failed_attempts >= self.config.max_failed_attempts:
                    self._record(conversation, turn_id, {"role": "assistant", "content": GIVE_UP_MESSAGE})
                    await queue.put(TextDelta(GIVE_UP_MESSAGE))
                    break
            finished = trace.finish()
            self.store.save_trace(turn_id, finished)
            await queue.put(TraceReady(finished))
            await queue.put(TurnEnd())

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

    async def _emit_display(
        self, conversation: Conversation, turn_id: int, message_id: int, tool_content: str, queue: asyncio.Queue,
        call_arguments: str = "{}", request: str = "", trace: TraceBuilder | None = None,
    ):
        try:
            payload = json.loads(tool_content)
        except ValueError:
            return
        if not isinstance(payload, dict):
            return
        if payload.get("success") and payload.get("result_table") and "display" not in payload:
            try:
                title = json.loads(call_arguments or "{}").get("title", "")
            except ValueError:
                title = ""
            self.store.add_result_table(
                conversation.id, payload["result_table"], title, payload.get("columns", []), request
            )
        display = payload.get("display")
        if not display:
            return
        owned = self.store.owns_result_table(conversation.id, display["result_table"])
        if display["kind"] == "chart":
            source = display["spec"]
            table = display["result_table"]
            if owned:
                self.store.add_display(
                    conversation.id, turn_id, message_id, "chart", table, {"spec": source}
                )
            resolved = resolve_chart_data(source, table)
            if trace:
                trace.add("chart_spec", result_table=table, source_spec=source, spec=resolved)
            await queue.put(DisplayEvent("chart", table, spec=resolved, source_spec=source))
            return
        if owned:
            self.store.add_display(
                conversation.id, turn_id, message_id, display["kind"], display["result_table"],
                {"columns": display["columns"]},
            )
            await queue.put(DisplayEvent(display["kind"], display["result_table"], display["columns"]))
