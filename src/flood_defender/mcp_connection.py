"""The Agent's connection to its one long-lived MCP server (stdio subprocess).

Interface: `start()`, `tools`, `call_tool(name, arguments, conversation_id)`, `aclose()`, and
async-context-manager support. The MCP client's stdio and session contexts must be entered and
exited in the same task, so a dedicated owner task does both; `start()` and `aclose()` only
signal it and wait, so they may be called from any task. Tool calls from any task share the one
session (the MCP client multiplexes concurrent requests by id).

The Conversation id travels in each call's request `_meta`, never in the launch environment
or in tool arguments (ADR 0002).
"""

import asyncio

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.types import CallToolResult

from flood_defender.mcp_server import META_CONVERSATION_ID


def _openai_tool(t) -> dict:
    return {
        "type": "function",
        "function": {"name": t.name, "description": t.description or "", "parameters": t.inputSchema},
    }


class McpConnection:
    def __init__(self, params: StdioServerParameters):
        self._params = params
        self._session: ClientSession | None = None
        self._tools: list[dict] = []
        self._owner: asyncio.Task | None = None
        self._ready = asyncio.Event()
        self._stop = asyncio.Event()
        self._start_error: BaseException | None = None

    @property
    def tools(self) -> list[dict]:
        """The server's tools in OpenAI function-tool shape, cached at `start()`."""
        return self._tools

    async def start(self) -> None:
        """Launch the subprocess, initialise the session, cache the tool list."""
        if self._owner is not None:
            raise RuntimeError("MCP connection already started")
        self._owner = asyncio.create_task(self._own())
        await self._ready.wait()
        if self._start_error is not None:
            await asyncio.gather(self._owner, return_exceptions=True)
            raise self._start_error

    async def _own(self) -> None:
        try:
            async with stdio_client(self._params) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    self._tools = [_openai_tool(t) for t in (await session.list_tools()).tools]
                    self._session = session
                    self._ready.set()
                    await self._stop.wait()
        except BaseException as e:
            if not self._ready.is_set():
                self._start_error = e
            if isinstance(e, asyncio.CancelledError):
                raise
        finally:
            self._session = None
            self._ready.set()

    async def call_tool(self, name: str, arguments: dict, conversation_id: str) -> CallToolResult:
        """Call a tool, sending the Conversation id in request `_meta`."""
        if self._session is None:
            raise RuntimeError("MCP connection is not running")
        return await self._session.call_tool(name, arguments, meta={META_CONVERSATION_ID: conversation_id})

    async def aclose(self) -> None:
        """Shut the server down cleanly and reap the process."""
        self._stop.set()
        if self._owner is not None:
            await asyncio.gather(self._owner, return_exceptions=True)

    async def __aenter__(self) -> "McpConnection":
        await self.start()
        return self

    async def __aexit__(self, *exc) -> None:
        await self.aclose()
