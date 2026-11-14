# One long-lived MCP server per app; request context travels in `_meta`

The Agent runs one MCP server subprocess (stdio) for the life of the app, not one per Run. In the web app it starts at app startup and stops at shutdown; every AI Cell Run in every Notebook uses it. A per-Run server cost about 0.45 s of start and teardown on every Run and tied a subprocess's lifetime to a Run, which caused the leaked-subprocess bug.

Per-request context travels in each tool call's MCP request `_meta` under a namespaced key. Today that is only the AI Cell's row cap (`flood_agent/max_result_rows`). It is set by the Agent and never appears in the launch environment or in any tool's input schema, so the LLM can neither see nor set it. The launch environment holds static settings only. (The first design also sent the Conversation id, to isolate each Conversation's Result Tables. ADR 0003 superseded that: every Notebook may read and show every live table in the results database, so the server never read the id, and it was dropped with Conversations. The Agent, not the server, records which Cell and Run own a new Result Table and which tables an AI Cell's system prompt lists.)

A dedicated owner task inside the connection module (`flood_agent.mcp_connection`) enters and exits the MCP client's transport (stdio in production) and session contexts, because they must be exited in the task that entered them. `start()` and `aclose()` only signal that task, so they may be called from any task.

Tools run off the server's event loop: each tool is an async function that hands its sync body to a worker thread, because FastMCP calls sync tool functions directly on its loop and one slow `run_query` (bounded only by the query timeout) would otherwise stall every other call. Each call opens its own SQLite connections, so threads share no state.

## Consequences

- The Agent has an async lifecycle (`start()` / `aclose()`, or `async with`); running a cell on an unstarted Agent raises.
- A client disconnect cancels the Run but leaves the server running.
- If the server has died, the next Run replaces it before calling the LLM; a tool call that fails because it died mid-Run takes the normal tool-error path, and a failed restart is an ordinary Run failure (#17).
- Complements ADR 0001: the server is still a separate process launched with only static settings.
