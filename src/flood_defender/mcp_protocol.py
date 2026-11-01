"""Wire constants shared by the MCP client (`mcp_connection`) and server (`mcp_server`)."""

# Request `_meta` key carrying the calling Conversation's id (ADR 0002).
META_CONVERSATION_ID = "flood_defender/conversation_id"

# Tools that take LLM-written SQL; failed attempts count toward the per-turn retry limit.
QUERY_TOOLS = ("run_query", "run_count_query")
