# Prompt caching per curated model (#66, part of #55)

Question: for each model in the eval runner's default list and the app's curated list, does OpenRouter caching happen automatically, need request fields, or not exist; and what are the minimum size, TTL and price multipliers?

Read 2026-11-06. Sources:

- **[PC]** OpenRouter Prompt Caching guide, https://openrouter.ai/docs/guides/best-practices/prompt-caching (fetched as `.md`).
- **[M]** `GET https://openrouter.ai/api/v1/models` (model-level `pricing`, `supported_parameters`).
- **[E]** `GET https://openrouter.ai/api/v1/models/{id}/endpoints` (per-provider `pricing`, `supports_implicit_caching`). This is what each model's OpenRouter page renders.

## Scope

Eval runner `CANDIDATE_MODELS` (`src/flood_agent/evalset/runner.py`): `anthropic/claude-sonnet-5.5`, `deepseek/deepseek-v4-pro-0813`, `z-ai/glm-5.3`, `qwen/qwen3.8-27b`. App curated list (`src/flood_agent/defaults.toml`, map #45 landed) adds `anthropic/claude-opus-5.5`, `anthropic/claude-haiku-5.5`, `openai/gpt-5.6-terra`, `openai/gpt-5.6-sol`, `openai/gpt-5.6-luna`. Nine models in all.

## Findings table

| Model prefix | Caching mode | Required request fields | Min size | TTL | Write / read price (x input) | Sources |
|---|---|---|---|---|---|---|
| `anthropic/claude-sonnet-5.5` | Explicit (off without a field) | Top-level `"cache_control": {"type": "ephemeral"}` (auto-advancing breakpoint), or per-block `cache_control` (max 4). Optional `"ttl": "1h"` | 512 tokens | 5 min default, 1 h optional | Write 1.25x (5 min), 2x (1 h). Read: guide says 0.1x; [M] shows 0.05x (0.0000001 / 0.000002) | [PC] Anthropic Claude section; [M] |
| `anthropic/claude-opus-5.5` | Explicit | Same as Sonnet | 512 tokens | 5 min / 1 h | Write 1.25x / 2x (0.000005, 0.000008 vs 0.000004 input). Read 0.05x per [M] (0.0000002 / 0.000004); guide says 0.1x | [PC]; [M] |
| `anthropic/claude-haiku-5.5` | Explicit | Same as Sonnet | 512 tokens | 5 min / 1 h | Write 1.25x / 2x. Read 0.1x. Prices double-tier above 100k prompt tokens, multipliers unchanged | [PC]; [M] |
| `openai/gpt-5.6-terra` | Automatic (implicit), always on | None. Optional: `prompt_cache_key`/`session_id` for sticky routing; `prompt_cache_options` + `prompt_cache_breakpoint` for explicit control | 1024 tokens | 30 min minimum (default); 12 h with `"ttl": "12h"` in explicit mode | **Write 1.25x even on automatic caching** (GPT-5.6 and later); 12 h write 2.5x. Read 0.1x ([M]: 0.0000002 / 0.000002). Prices double above 272k prompt tokens, multipliers unchanged | [PC] OpenAI section, "Explicit prompt caching", "Disabling prompt caching"; [M]; [E] |
| `openai/gpt-5.6-sol` | Automatic, as Terra | None | 1024 tokens | 30 min / 12 h | Write 1.25x; read 0.1x ([M]: 0.0000004 / 0.000004) | as above |
| `openai/gpt-5.6-luna` | Automatic, as Terra | None | 1024 tokens | 30 min / 12 h | Write 1.25x; read 0.1x ([M]: 0.00000002 / 0.0000002) | as above |
| `deepseek/deepseek-v4-pro-0813` | Automatic | None | Unknown (guide gives none) | Unknown (guide gives none) | Guide: write same as input, read 0.1x. [E]: all 25 endpoints `supports_implicit_caching: true`; actual read ratio varies by endpoint (about 0.03x on DeepSeek's own endpoint, 0.95x on the cheapest, Wafer) | [PC] DeepSeek section; [E] |
| `z-ai/glm-5.3` | Automatic on 45 of 46 endpoints | None. Passing `session_id` improves hit rate (OpenRouter sends Z.AI a session affinity key derived from it) | Unknown | Unknown | Write free; read "typically about 0.2x" per guide. [E] ratio varies from about 0.1x to 1.0x by endpoint | [PC] Z.AI section; [E] |
| `qwen/qwen3.8-27b` | Automatic on 22 of 24 endpoints (implicit); not in the guide's explicit list | None for the implicit endpoints. The Alibaba endpoint has `supports_implicit_caching: false` and a write price (0.00000053125, 1.25x its input), so it is the explicit kind and would need `cache_control`; Venice lists no cache read price | Unknown | Unknown (guide: 5 min for Alibaba explicit writes) | Alibaba explicit: write 1.25x, read 0.1x ([PC]; [E]: 0.000000085 / 0.000000425 = 0.2x on this model, so the guide's 0.1x does not match this endpoint). Implicit endpoints: read ratio 0.1x to 1.0x, write none | [PC] Alibaba Qwen section; [E] |

Additional facts that apply to every row ([PC]):

- Sticky routing keeps a conversation on the provider that holds its cache. It uses `session_id` (body field or `x-session-id` header, max 256 chars), else `prompt_cache_key`, else a hash of the first system/developer message plus first non-system message. Sessions expire after 60 minutes of inactivity. Without `session_id`, stickiness starts only after a cache hit is seen; with it, from the first successful request. Stickiness only applies when the provider's cache read price is cheaper than normal input.
- Hits are reported in `usage.prompt_tokens_details.cached_tokens`, writes in `cache_write_tokens`; `cache_discount` in the body gives the saving.
- Reasoning pass-back: the caching guide says nothing about whether returning reasoning changes cache hits for any model. **Unknown**; not established by these sources.

## Unknowns (not guessed)

- Minimum cacheable size and TTL for DeepSeek V4, GLM 5.3 and Qwen3.8 27B: the guide gives none and the endpoint metadata has no field for them. Provider docs (e.g. https://docs.z.ai/guides/capabilities/cache) were not read; they are the next step if these matter.
- Whether the Qwen3.8 27B implicit endpoints charge for writes: [E] shows no write price, taken here as free but not stated by the guide.
- Whether reasoning pass-back affects hits (see above).
- Which Qwen3.8 27B endpoint OpenRouter will route to; the answer changes the mode. See the recommendation.

## Notes on discrepancies

- Claude read multiplier: guide text says 0.1x; the 5.5 Sonnet and Opus prices in [M]/[E] give 0.05x. Trust the prices for cost estimates.
- `qwen/qwen3.8-27b`: `defaults.toml` has `context_window = 1_024_000`, which is the model-level `context_length` in [M] and the largest endpoint; most endpoints (and `top_provider.context_length` in [M]) are 262,144, and some are 131,072 or 65,536. The meter from map #45 could understate pressure on routed endpoints. Out of scope here but worth a separate look.
- GLM 5.3 also has one explicit-cache endpoint (Amazon Bedrock: `supports_implicit_caching: false`, write price 0.0000021). Treat as the same class of risk as the Qwen Alibaba endpoint.

## Recommendations

### Needs a ticket

**1. Claude (Sonnet 5.5, Opus 5.5, Haiku 5.5): send top-level `cache_control`.** This is the already-planned Claude ticket (#62 to #65 under #55). Rule: for model ids starting `anthropic/`, add `"cache_control": {"type": "ephemeral"}` to the request body in `OpenRouterClient.stream_chat`. Default 5-minute TTL (agent turns are seconds apart; 1 h costs 2x to write). Minimum 512 tokens for all three, so the system prompt plus KB should clear it. Also send `session_id` (one per Conversation) to pin the provider.

**2. GPT-5.6 (Terra, Sol, Luna): no field needed for hits, but there is a cost trap.** Caching is automatic and writes are billed at 1.25x on every prompt of 1024 tokens or more, whether or not it is ever re-read. For a multi-step agent loop the prefix is re-read within the 30-minute TTL, so automatic caching pays for itself and nothing needs turning off. The ticket is small: extend the same prefix helper with an `openai/` entry that sends `session_id` (improves sticky routing so reads actually land) and surfaces `cache_write_tokens` as well as `cached_tokens` in the Trace/cost so the 1.25x write cost is visible. Do not send `prompt_cache_options` unless a measurement shows a benefit (explicit mode with 12 h TTL costs 2.5x to write). Optional, only if eval cost shows one-shot prompts dominating: explicit mode with no breakpoints disables caching ([PC] "Disabling prompt caching").

**3. Qwen3.8 27B: decide how to handle the explicit-cache Alibaba endpoint.** 22 of 24 endpoints cache implicitly, so by default there is nothing to send. The guide's explicit-caching model list does not include `qwen3.8-27b`, but the Alibaba endpoint's metadata (no implicit caching, a write price) says it behaves like one. The ticket: (a) pass `session_id` so sticky routing stabilises on one endpoint; (b) measure on the golden set whether `cached_tokens` is non-zero through OpenRouter's default routing; (c) if it routes to Alibaba and `cached_tokens` stays 0, add `qwen/` to the prefix helper with top-level or block `cache_control` (Alibaba documents block-level syntax identical to Anthropic's; whether the top-level field is honoured for Alibaba is **unknown**), or pin `provider.order`/ignore that endpoint. Do (b) first; implement (c) only if it shows a problem.

### Nothing to do

- **DeepSeek V4 Pro (`deepseek/`)**: automatic on every endpoint; no request fields. Optionally covered by the generic `session_id` addition but no DeepSeek-specific work.
- **GLM 5.3 (`z-ai/`)**: automatic; `session_id` helps hit rate (covered by the generic `session_id` addition, not a GLM-specific ticket). The one Bedrock endpoint is a routing edge case, same handling as Qwen if it ever shows as zero hits.

### Shape of the helper

Make the prefix helper a small table keyed by model-id prefix returning extra request-body fields, so the Claude ticket's `anthropic/` entry is the only one that adds `cache_control` today, and `session_id` is sent for all models. Add a Trace field for `cached_tokens` and `cache_write_tokens` (answers #55's "Should the Trace show cached tokens?" for GPT-5.6, where writes cost extra).
