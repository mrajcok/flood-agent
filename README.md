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
cp .env.example .env     # then set OPENROUTER_API_KEY in .env (never commit it)
uv run python -m flood_defender                    # http://127.0.0.1:8000
```

## Settings

Every setting has a default in `src/flood_defender/defaults.toml`, the only place defaults
are written. Override any of them, highest priority first, with an environment variable, a
`.env` file in the working directory, or (lowest) that defaults file. Names are the keys in
upper case (`QUERY_TIMEOUT_S`, `MAX_LLM_STEPS`, ...); blank values count as unset and `$`
is never interpolated. Validation errors name the offending setting.

`.env.example` documents every setting and is generated from `flood_defender/config.py`
and `defaults.toml`; a test fails if it is stale. After changing either, regenerate it:

```
uv run python -m flood_defender.config > .env.example
```

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
text prefixed with `'`). Both are Agent endpoints, not LLM tools. The `/api/results/...`
endpoints are not scoped per Conversation by design: Flood Defender is single-user and local, with no authentication.

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

## Eval set and runner (on demand, not CI)

`src/flood_defender/evalset/golden.yaml` holds ~30 golden questions with reference SQL
(many target the injected attacks of the default seed-1 database). `pytest` checks that
every reference query runs and that attack answers agree with `attacks.json`, and tests
the runner with the scripted fake LLM; it never calls OpenRouter.

To compare models against real OpenRouter (costs money):

```
uv run python -m flood_defender.generator data/telemetry.db        # default seed 1, full scale
export OPENROUTER_API_KEY=...
uv run python -m flood_defender.evalset --db data/telemetry.db \
    --models anthropic/claude-sonnet-5.5 deepseek/deepseek-v4-pro-0813 z-ai/glm-5.3 qwen/qwen3.8-27b \
    --out eval-report                                              # writes eval-report.md / .json
```

The eval always runs with the limits in `defaults.toml`, ignoring your `.env` and exported
variables, so runs are comparable between machines; the API key (`.env` is fine) and the
default `--db` / `--semantic-layer` paths are the only settings it reads from them.

`--models` defaults to those four; `--questions id ...` runs a subset. Per model it reports
correctness, retries (failed `run_query` attempts), latency and cost (OpenRouter-reported).

Comparison rules (`evalset/compare.py`): a question is correct if any successful Result Table
the LLM created matches the reference. Column names and order are ignored; extra columns are
allowed (counted in the JSON); every reference column must appear with identical values and
rows must line up. Rows are order-insensitive unless the question sets `ordered: true`.
Numbers: int/float interchangeable, relative tolerance 1e-6 (per-question `tolerance`);
strings exact and case-sensitive; NULL equals NULL.
The runner reads Result Tables itself (Agent-side harness); the LLM receives only what the
Agent normally sends.
