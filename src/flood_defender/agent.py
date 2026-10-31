"""The Agent: runs the LLM tool loop for one turn and emits an event stream.

The HTTP/SSE layer is a thin adapter over `Agent.run_turn`. The LLM client is injected.
"""

import asyncio
import json
import sys
from contextlib import AsyncExitStack
from dataclasses import dataclass, field
from pathlib import Path
from typing import AsyncIterator

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from flood_defender import queries
from flood_defender.llm import LLMClient, TextChunk, ToolCallChunk
from flood_defender.semantic_layer import SemanticLayer
from flood_defender.store import ConversationStore

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
    columns: list[str]


@dataclass
class TraceEntry:
    """Trace data for ticket #10 to pick up. `query_error` carries the raw SQLite error,
    which is never sent to the LLM, next to the sanitised error the LLM did see."""

    kind: str
    sql: str | None = None
    raw_error: str | None = None
    sanitized_error: str | None = None


@dataclass
class TurnEnd:
    pass


Event = TextDelta | DisplayEvent | TraceEntry | TurnEnd

GIVE_UP_MESSAGE = (
    "Sorry, I couldn't build a working query for that after several attempts. "
    "Please try rephrasing your question."
)


def _openai_tool(t) -> dict:
    return {
        "type": "function",
        "function": {"name": t.name, "description": t.description or "", "parameters": t.inputSchema},
    }


class Agent:
    def __init__(self, config: AgentConfig, llm: LLMClient):
        self.config = config
        self.llm = llm
        self.semantic_layer = SemanticLayer(config.semantic_layer)
        self.store = ConversationStore(config.app_db or config.results_db.parent / "app.db")

    def open_conversation(self, conversation_id: str) -> Conversation:
        """Load a saved Conversation (KeyError if unknown)."""
        if not self.store.exists(conversation_id):
            raise KeyError(conversation_id)
        return Conversation(conversation_id, self.store.messages(conversation_id))

    def fetch_rows(self, result_table: str, columns: list[str] | None = None) -> dict:
        """Internal data path used by the row-JSON endpoint. Not an LLM tool."""
        return queries.fetch_rows(self.config.results_db, result_table, columns)

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
        if conversation.id is None:
            conversation.id = self.store.create_conversation(user_message[:60])
        turn_id = self.store.start_turn(conversation.id, user_message)
        params = StdioServerParameters(
            command=sys.executable,
            args=["-m", "flood_defender.mcp_server"],
            env={
                "TELEMETRY_DB": str(self.config.telemetry_db),
                "RESULTS_DB": str(self.config.results_db),
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
            for _ in range(MAX_LLM_STEPS):
                messages = [{"role": "system", "content": self.semantic_layer.system_prompt()}]
                messages += conversation.messages
                text, calls = "", []
                async for chunk in self.llm.stream_chat(messages, tools):
                    if isinstance(chunk, TextChunk):
                        text += chunk.text
                        await queue.put(TextDelta(chunk.text))
                    elif isinstance(chunk, ToolCallChunk):
                        calls.append(chunk)
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
                    result = await session.call_tool(c.name, args)
                    content = "".join(b.text for b in result.content if hasattr(b, "text"))
                    if c.name == "run_query":
                        content, failed = await self._split_query_error(content, args, queue)
                        failed_attempts += failed
                    message_id = self._record(
                        conversation, turn_id, {"role": "tool", "tool_call_id": c.id, "content": content}
                    )
                    await self._emit_display(conversation, turn_id, message_id, content, queue)
                if failed_attempts >= self.config.max_failed_attempts:
                    self._record(conversation, turn_id, {"role": "assistant", "content": GIVE_UP_MESSAGE})
                    await queue.put(TextDelta(GIVE_UP_MESSAGE))
                    break
            await queue.put(TurnEnd())

    async def _split_query_error(self, content: str, args: dict, queue) -> tuple[str, int]:
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
        return json.dumps(meta), 1

    def _record(self, conversation: Conversation, turn_id: int, message: dict) -> int:
        conversation.messages.append(message)
        return self.store.add_message(conversation.id, turn_id, message)

    async def _emit_display(
        self, conversation: Conversation, turn_id: int, message_id: int, tool_content: str, queue: asyncio.Queue
    ):
        try:
            payload = json.loads(tool_content)
        except ValueError:
            return
        if not isinstance(payload, dict):
            return
        if payload.get("success") and payload.get("result_table") and "display" not in payload:
            self.store.add_result_table(conversation.id, payload["result_table"])
        display = payload.get("display")
        if display and self.store.owns_result_table(conversation.id, display["result_table"]):
            self.store.add_display(
                conversation.id, turn_id, message_id, display["kind"], display["result_table"],
                {"columns": display["columns"]},
            )
            await queue.put(DisplayEvent(display["kind"], display["result_table"], display["columns"]))
