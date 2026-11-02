"""Wire constants shared by the MCP client (`mcp_connection`) and server (`mcp_server`)."""

# Request `_meta` key carrying the calling Conversation's id (ADR 0002).
META_CONVERSATION_ID = "flood_agent/conversation_id"

# Request `_meta` key carrying the row cap the user chose for this turn. Absent: the server's default.
META_MAX_RESULT_ROWS = "flood_agent/max_result_rows"

# Tools that take LLM-written SQL; failed attempts count toward the per-turn retry limit.
QUERY_TOOLS = ("run_query", "run_count_query")
