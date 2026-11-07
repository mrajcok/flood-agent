# OpenRouter model controls: effort, context windows, usage reporting

Research for issue #46 (map #45). Researched 2026-11-06. No prior research-notes convention exists in the repo, so this lives in `docs/research/`.

## Sources

- [Reasoning tokens](https://openrouter.ai/docs/use-cases/reasoning-tokens) (OpenRouter docs)
- [Usage accounting](https://openrouter.ai/docs/use-cases/usage-accounting) (OpenRouter docs)
- [Streaming](https://openrouter.ai/docs/api-reference/streaming) (OpenRouter docs)
- `GET https://openrouter.ai/api/v1/models`, fetched 2026-11-06 (457 entries). Model facts below are read from this snapshot and will drift; re-query before hard-coding.

## 1. Setting Effort per request

Send a unified `reasoning` object in the request body:

```json
"reasoning": { "effort": "high" }
```

- Fields: `effort`, `max_tokens` (use one, not both), `enabled`, `exclude`.
- Levels the docs name: `max`, `xhigh`, `high`, `medium`, `low`, `minimal`, `none`. Effort is a share of the output limit: max/xhigh about 95%, high 80%, medium 50%, low 20%, minimal 10%. `none` turns reasoning off.
- `enabled: true` alone means medium effort. `exclude: true` still bills reasoning tokens but omits them from the response (Chat Completions only).
- Anthropic models take reasoning only through this unified parameter. With effort, the budget is `max(min(max_output_tokens * ratio, 128000), 1024)`, and `max_output_tokens` must exceed the budget.
- Reasoning tokens count against `max_output_tokens` on most providers. A response can be cut off with no visible output while still billing the reasoning.
- Streaming: reasoning arrives in `choices[].delta.reasoning_details`; encrypted reasoning may show as `[REDACTED]`. The app currently ignores this delta (it only reads `content` and `tool_calls`), so enabling effort will not break parsing.

Which levels each model accepts is not guessed from the family; the `/api/v1/models` entry carries a per-model `reasoning` object:
`{"mandatory": bool, "supported_efforts": [...], "default_effort": "...", "default_enabled"?: bool}`.
The `supported_parameters` list also contains `reasoning`, `include_reasoning` and (for most) `reasoning_effort`. The docs page for the models endpoint was not fetchable (404), so the field semantics of `reasoning` are inferred from the live data: `mandatory: true` appears on models where reasoning cannot be turned off (and `none` is then absent from `supported_efforts`).

## 2. Candidates

All rows: `tools` is in `supported_parameters` (needed by this app), `reasoning` is supported. Context window is `context_length`; max output is `top_provider.max_completion_tokens`. Prices are USD per million tokens (prompt / completion).

| Model id | Context | Max output | Supported efforts | Default | Can disable | Price in/out |
|---|---|---|---|---|---|---|
| anthropic/claude-sonnet-5.5 (current) | 1,000,000 | 128,000 | max, xhigh, high, medium, low | high | no (mandatory) | 2 / 10 |
| anthropic/claude-opus-5.5 | 1,000,000 | 128,000 | max, xhigh, high, medium, low | high | no (mandatory) | 4 / 20 |
| anthropic/claude-haiku-5.5 | 1,000,000 | 128,000 | max, xhigh, high, medium, low, none | medium | yes | 0.10 / 0.50 |
| anthropic/claude-sonnet-5 | 1,000,000 | 128,000 | max, xhigh, high, medium, low, none | high | yes | 2 / 10 |
| anthropic/claude-opus-5 | 1,000,000 | 128,000 | max, xhigh, high, medium, low, none | high | yes | 5 / 25 |
| anthropic/claude-sonnet-4.6 | 1,000,000 | 128,000 | max, high, medium, low, none | medium | yes | 3 / 15 |
| openai/gpt-5.6-terra | 1,050,000 | 128,000 | max, xhigh, high, medium, low, none | medium | yes | 2 / 12 |
| openai/gpt-5.6-sol | 1,050,000 | 128,000 | max, xhigh, high, medium, low, none | medium | yes | 4 / 20 |
| openai/gpt-5.6-luna | 1,050,000 | 128,000 | max, xhigh, high, medium, low, none | medium | yes | 0.20 / 1.20 |
| google/gemini-3.8-flash | 1,048,576 | 65,536 | high, medium, low | medium | no (mandatory) | 0.825 / 4.125 |
| google/gemini-3.5-flash | 1,048,576 | 65,536 | high, medium, low, minimal | medium | no (mandatory) | 1.5 / 9 |
| x-ai/grok-4.7 | 500,000 | 450,000 | xhigh, high, medium, low | high | no (mandatory) | 2 / 6 |
| x-ai/grok-4.3 | 1,000,000 | 900,000 | high, medium, low, none | low | yes | 1.25 / 2.5 |

Notes:

- Not every model has effort levels. `anthropic/claude-haiku-4.5` and `google/gemini-2.5-pro` list `reasoning` support but no `supported_efforts` (budget-style via `max_tokens`). `deepseek/deepseek-v3.2` has `reasoning` but no `reasoning_effort` and no effort list. Qwen coder models list neither. A curated list should only offer an Effort control for models whose entry has a non-empty `supported_efforts`.
- Effort levels differ per model (for example Gemini has no `max`/`xhigh`, and `minimal` appears only on Gemini). The UI should read levels per model rather than share one fixed set.
- "Mandatory" models reject `none`; hide that option for them.
- Window sizes are 1M for the Claude 4.6+/5.x line, so context pressure is unlikely to be the binding constraint for the Trace.

## 3. Does streamed usage match what the Trace already reads?

Yes for `prompt_tokens`, with one wrinkle.

- Chat Completions usage fields: `prompt_tokens`, `completion_tokens`, `total_tokens`, `cost`, `cost_details.upstream_inference_cost`, `prompt_tokens_details` (`cached_tokens`, `cache_write_tokens`, `audio_tokens`), `completion_tokens_details.reasoning_tokens`. (Usage accounting docs.)
- Streaming: "every stream ends with an extra chunk that carries the `usage` object", sent just before `[DONE]` (Streaming docs).
- The usage accounting page says `stream_options` and the old `usage: {include: true}` flag are deprecated with no effect, and full usage is always returned. `llm.py` still sends both; harmless, and could be dropped.
- Wrinkle: the usage chunk is not an empty-`choices` chunk as in OpenAI's spec. OpenRouter documents that it contains one choice with a content-free `delta` repeating the terminal `finish_reason`, so `finish_reason` appears twice. `llm.py` reads `obj["usage"]` before looking at `choices`, and its choice handling tolerates an empty delta, so it handles both shapes. It reads `prompt_tokens`, `completion_tokens` and `cost`, all documented fields.
- `prompt_tokens` semantics: with prompt caching, `prompt_tokens_details.cached_tokens` is a subset reported alongside it; the docs do not say `prompt_tokens` excludes cached tokens, so treat `prompt_tokens` as total input. If the Trace later wants to separate cache reads, read `cached_tokens`.
- Reasoning tokens appear under `completion_tokens_details.reasoning_tokens` (not read today). Raising Effort will raise `completion_tokens` and `cost`, not `prompt_tokens`.
- Mid-stream errors: HTTP 200 is already committed, so failures arrive as a chunk with a top-level `error` (the app already raises `LLMError` on this). SSE comment lines start with `:` and must be skipped (the app skips any line not starting `data:`).

## Recommendation for the curated list

List the Claude 5.5 trio (Sonnet as default, Opus for hard cases, Haiku as cheap/fast), plus optionally one OpenAI (`gpt-5.6-terra`) and one Gemini or Grok model for comparison. All have tool calling, windows of at least 500k, and `supported_efforts`. Drive the Effort control from each model's `reasoning.supported_efforts` and `mandatory` fields in `/api/v1/models`, sending `reasoning: {"effort": ...}` only when the user picks one.
