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
    app_db: Path | None = None  # defaults to app.db beside the results database
    chart_row_cap: int = 10_000


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
class TurnEnd:
    pass


Event = TextDelta | DisplayEvent | TurnEnd


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
        self.store = ConversationStore(config.app_db or config.results_db.parent / "app.db")

    def open_conversation(self, conversation_id: str) -> Conversation:
        """Load a saved Conversation (KeyError if unknown)."""
        if not self.store.exists(conversation_id):
            raise KeyError(conversation_id)
        return Conversation(conversation_id, self.store.messages(conversation_id))

    def fetch_rows(self, result_table: str, columns: list[str] | None = None, **page_args) -> dict:
        """Internal data path used by the row-JSON endpoint. Not an LLM tool.

        Adds per-column display formats from the Semantic Layer."""
        data = queries.fetch_rows(self.config.results_db, result_table, columns, **page_args)
        data["formats"] = self.semantic_layer.column_formats(data["columns"])
        return data

    def iter_all_rows(self, result_table: str):
        """Internal data path for CSV export of the full Result Table. Not an LLM tool."""
        return queries.iter_all_rows(self.config.results_db, result_table)

    def fetch_chart_rows(self, result_table: str) -> list[dict]:
        """Internal data path for the chart-rows endpoint (chart cap). Not an LLM tool."""
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
        if conversation.id is None:
            conversation.id = self.store.create_conversation(user_message[:60])
        turn_id = self.store.start_turn(conversation.id, user_message)
        params = StdioServerParameters(
            command=sys.executable,
            args=["-m", "flood_defender.mcp_server"],
            env={
                "TELEMETRY_DB": str(self.config.telemetry_db),
                "RESULTS_DB": str(self.config.results_db),
                "CHART_ROW_CAP": str(self.config.chart_row_cap),
            },
        )
        async with AsyncExitStack() as stack:
            read, write = await stack.enter_async_context(stdio_client(params))
            session = await stack.enter_async_context(ClientSession(read, write))
            await session.initialize()
            tools = [_openai_tool(t) for t in (await session.list_tools()).tools]

            self._record(conversation, turn_id, {"role": "user", "content": user_message})
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
                    result = await session.call_tool(c.name, json.loads(c.arguments or "{}"))
                    content = "".join(b.text for b in result.content if hasattr(b, "text"))
                    message_id = self._record(
                        conversation, turn_id, {"role": "tool", "tool_call_id": c.id, "content": content}
                    )
                    await self._emit_display(conversation, turn_id, message_id, content, queue)
            await queue.put(TurnEnd())

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
            await queue.put(
                DisplayEvent("chart", table, spec=resolve_chart_data(source, table), source_spec=source)
            )
            return
        if owned:
            self.store.add_display(
                conversation.id, turn_id, message_id, display["kind"], display["result_table"],
                {"columns": display["columns"]},
            )
            await queue.put(DisplayEvent(display["kind"], display["result_table"], display["columns"]))
