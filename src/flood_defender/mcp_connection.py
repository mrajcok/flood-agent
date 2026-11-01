"""The Agent's connection to its one long-lived MCP server.

Interface: `start()`, `tools`, `alive`, `call_tool(name, arguments, conversation_id)`, `aclose()`.
A connection starts once and cannot be restarted after `aclose()`; make a new one. The MCP client's stdio and session contexts must be entered and
exited in the same task, so a dedicated owner task does both; `start()` and `aclose()` only
signal it and wait, so they may be called from any task. Tool calls from any task share the one
session (the MCP client multiplexes concurrent requests by id).

The Conversation id travels in each call's request `_meta`, never in the launch environment
or in tool arguments (ADR 0002).
"""

import asyncio
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.types import CallToolResult

from flood_defender.mcp_protocol import META_CONVERSATION_ID, META_MAX_RESULT_ROWS


def _openai_tool(t) -> dict:
    return {
        "type": "function",
        "function": {"name": t.name, "description": t.description or "", "parameters": t.inputSchema},
    }


Transport = Callable[[], AbstractAsyncContextManager[tuple]]
"""Opens the client's (read, write) streams to a server, and closes them (and the server) on exit."""


def stdio_transport(params: StdioServerParameters) -> Transport:
    """A server launched as a stdio subprocess: the production transport."""
    return lambda: stdio_client(params)


class McpConnection:
    def __init__(self, transport: Transport):
        self._transport = transport
        self._session: ClientSession | None = None
        self._tools: list[dict] = []
        self._owner: asyncio.Task | None = None
        self._ready = asyncio.Event()
        self._stop = asyncio.Event()
        self._start_error: BaseException | None = None
        self._read = None
        self._closed = False

    @property
    def tools(self) -> list[dict]:
        """The server's tools in OpenAI function-tool shape, cached at `start()`."""
        return self._tools

    @property
    def alive(self) -> bool:
        """True while the server process and session are up (False once either has died).

        The MCP client exposes no public liveness signal; the stdout reader closes its end of the
        read stream when the process exits, which anyio's public `statistics()` reports."""
        if self._session is None or self._owner is None or self._owner.done() or self._read is None:
            return False
        # The server's stdout reader closes its end when the process exits.
        return self._read.statistics().open_send_streams > 0

    async def start(self) -> None:
        """Start the server, initialise the session, cache the tool list."""
        if self._owner is not None or self._closed:
            raise RuntimeError("MCP connection already started or closed")
        self._owner = asyncio.create_task(self._own())
        try:
            await self._ready.wait()
        except BaseException:
            # Cancelled (or otherwise interrupted) mid-start: cancel the owner so the subprocess
            # is reaped rather than orphaned.
            await self._shutdown(cancel=True)
            raise
        if self._start_error is not None:
            await asyncio.gather(self._owner, return_exceptions=True)
            raise self._start_error

    async def _own(self) -> None:
        try:
            async with self._transport() as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    self._tools = [_openai_tool(t) for t in (await session.list_tools()).tools]
                    self._session = session
                    self._read = read
                    self._ready.set()
                    await self._stop.wait()
        except BaseException as e:
            if not self._ready.is_set():
                self._start_error = e
            if isinstance(e, asyncio.CancelledError):
                raise
            # Any other error after startup (e.g. the server died) just ends the connection:
            # `alive` turns False and the Agent replaces it; there is no caller to re-raise to.
        finally:
            self._session = None
            self._ready.set()

    async def call_tool(
        self, name: str, arguments: dict, conversation_id: str, max_result_rows: int | None = None
    ) -> CallToolResult:
        """Call a tool, sending the Conversation id (and this turn's row cap, if any) in request `_meta`."""
        if self._session is None:
            raise RuntimeError("MCP connection is not running")
        meta: dict = {META_CONVERSATION_ID: conversation_id}
        if max_result_rows is not None:
            meta[META_MAX_RESULT_ROWS] = max_result_rows
        return await self._session.call_tool(name, arguments, meta=meta)

    async def aclose(self) -> None:
        """Shut the server down cleanly (a subprocess is reaped)."""
        await self._shutdown(cancel=False)

    async def _shutdown(self, cancel: bool) -> None:
        self._closed = True
        self._stop.set()
        if self._owner is not None:
            if cancel:
                self._owner.cancel()
            await asyncio.gather(self._owner, return_exceptions=True)
