# One long-lived MCP server per app; request context travels in `_meta`

The Agent runs one MCP server subprocess (stdio) for the life of the app, not one per turn. In the web app it starts at app startup and stops at shutdown; every turn in every Conversation uses it. A per-turn server cost about 0.45 s of start and teardown on every turn and tied a subprocess's lifetime to a turn, which caused the leaked-subprocess bug.

Per-request context, such as the Conversation id, travels in each tool call's MCP request `_meta` under a namespaced key (`flood_defender/conversation_id`). It is set by the Agent and never appears in the launch environment or in any tool's input schema, so the LLM can neither see nor set it. The launch environment holds static settings only. A call with a missing or empty id owns no Result Tables (fail closed), so Conversation isolation is as strong as with one server per Conversation.

A dedicated owner task inside the connection module (`flood_defender.mcp_connection`) enters and exits the MCP client's stdio and session contexts, because they must be exited in the task that entered them. `start()` and `aclose()` only signal that task, so they may be called from any task.

Tools must run off the server's event loop (worker threads), because FastMCP calls sync tool functions on its loop and one slow `run_query` would otherwise stall every Conversation's calls. (Tracked in #18.)

## Consequences

- The Agent has an async lifecycle (`start()` / `aclose()`, or `async with`); `run_turn` on an unstarted Agent raises.
- A client disconnect cancels the turn but leaves the server running.
- Restarting a dead server is handled separately (#17).
- Complements ADR 0001: the server is still a separate process launched with only static settings.
