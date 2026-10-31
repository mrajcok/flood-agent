# Flood Defender

Chat-driven flow telemetry analysis where no telemetry data passes through the LLM
(see `GLOSSARY.md` and `docs/adr/0001-no-telemetry-data-through-the-llm.md`).

This is the tracer bullet: ask a question, get streamed text plus an inline table Display.

## Setup

Requires [uv](https://docs.astral.sh/uv/) (Python 3.11+; the MCP SDK is pinned to 1.x).

```
uv sync
uv run python -m flood_defender.generator data/telemetry.db   # 7 days, ~2M Flow Records, 3 injected attacks;
                                                              # also writes the window into semantic_layer.yaml and data/telemetry.db.attacks.json
```

## Run

```
export OPENROUTER_API_KEY=...                      # never commit this
export OPENROUTER_MODEL=anthropic/claude-sonnet-5.5   # optional; this is the default
uv run python -m flood_defender                    # http://127.0.0.1:8000
```

Optional env: `TELEMETRY_DB` (default `data/telemetry.db`), `RESULTS_DB`
(`data/results.db`), `APP_DB` (`data/app.db`; Conversations), `SEMANTIC_LAYER` (`semantic_layer.yaml`), `HOST`, `PORT`.

## Test

```
uv run pytest
```

Tests run at the Agent turn seam: a real MCP server subprocess and real SQLite files,
with only the OpenRouter client replaced by a scripted fake (`tests/conftest.py`).
No network or credentials needed.

## Table Displays

Tables are paginated (50 rows/page, max 500) and sortable on any column by the Agent:
`GET /api/results/<rt>/rows?page=&page_size=&sort=<col>&dir=asc|desc` re-queries the
Result Table; `sort` must exactly match a column of that Result Table or the request is a
400. `GET /api/results/<rt>/csv` streams the full Result Table (raw values, formula-leading
text prefixed with `'`). Both are Agent endpoints, not LLM tools.

Formatting comes from Semantic Layer column metadata (`tables.flows.columns.<col>`):
`enum` -> names (proto), `enum_bits` -> flag letters (S, SA, FPA), `unit: bytes` -> decimal
KB/MB/GB/TB, `unit: epoch ms ...` -> UTC timestamps (never local time). A Result Table
column is matched to its source column by **identical name** (`proto`, `bytes`, `etime`);
aliased or derived columns (`SUM(bytes) AS total_bytes`) are shown plain. Pure formatting
functions live in `static/format.js` (tested with `node --test` via pytest).

## Layout

- `agent.py` - `Agent.run_turn(conversation, message)` yields `TextDelta`, `DisplayEvent`, `TurnEnd`.
- `llm.py` - `LLMClient` protocol and the OpenRouter implementation.
- `mcp_server.py` - stdio MCP server; the only LLM tools are `run_query` and `show_table`.
- `queries.py` - read-only Telemetry Database access, CTAS into the results DB, internal row access.
- `web.py` + `templates/` - FastAPI, Jinja, htmx (SSE), Alpine table component.
- `semantic_layer.yaml` - hand-authored Semantic Layer rendered into the system prompt.
