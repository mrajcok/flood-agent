# How reasoning and Effort work across the curated models

Research for issue #50 (map #45). Researched 2026-11-06. Builds on `docs/research/openrouter-model-controls.md` (branch `origin/research/openrouter-model-controls`, issue #46), which covers how to send `reasoning` and how streamed usage looks; this note does not repeat that and adds the nine curated models, vendor behaviour, and what Effort does to cost and `completion_tokens`. Findings only; the choice of list and the UI decision belong to the curated-list ticket.

## Sources, and how to read the labels

Every fact below is tagged with its owner:

- **[OR-models]** = live `GET https://openrouter.ai/api/v1/models`, fetched 2026-11-06 (458 entries). Fields: `context_length`, `top_provider.max_completion_tokens`, `supported_parameters`, `reasoning`. Will drift; re-query before hard-coding.
- **[OR-docs]** = OpenRouter docs, [Reasoning tokens](https://openrouter.ai/docs/use-cases/reasoning-tokens), including its section "Discovering per-model reasoning options" (this page documents the `reasoning` object; the earlier note could only infer it).
- **[Anthropic]** = [Effort](https://platform.claude.com/docs/en/build-with-claude/effort) (Claude API docs).
- **[OpenAI]** = [Reasoning guide](https://developers.openai.com/api/docs/guides/reasoning).
- **[DeepSeek]** = [Thinking mode](https://api-docs.deepseek.com/guides/thinking_mode).
- **[Z.ai]** = [Thinking mode](https://docs.z.ai/guides/capabilities/thinking-mode).
- **[Qwen]** = [Qwen3.8-27B model card](https://huggingface.co/Qwen/Qwen3.8-27B) (Hugging Face, published by Qwen).

Vendor pages were read through a summarising fetch tool, so quotes are as returned by it; treat exact wording as close, not verbatim.

## 1. Summary table (what OpenRouter will accept)

All nine list `tools` and `tool_choice` in `supported_parameters` [OR-models] and `reasoning`, `include_reasoning`, `reasoning_effort`. "Mandatory" is the `reasoning.mandatory` flag. Efforts are listed highest first, as OpenRouter returns them [OR-docs].

| Model | Context window | Max output | Tools | Reasoning can be turned off? | Effort levels OpenRouter accepts | Default effort |
|---|---|---|---|---|---|---|
| `anthropic/claude-sonnet-5.5` | 1,000,000 | 128,000 | yes | no (mandatory) | max, xhigh, high, medium, low | high |
| `anthropic/claude-opus-5.5` | 1,000,000 | 128,000 | yes | no (mandatory) | max, xhigh, high, medium, low | high (see conflict, section 3) |
| `anthropic/claude-haiku-5.5` | 1,000,000 | 128,000 | yes | yes (`none`) | max, xhigh, high, medium, low, none | medium |
| `deepseek/deepseek-v4-pro-0813` | 1,048,576 | 943,718 | yes | yes (`none`) | max, high, low, none | high |
| `z-ai/glm-5.3` | 1,048,576 | 943,718 | yes | no (mandatory) | max, high, low | max |
| `qwen/qwen3.8-27b` | 1,024,000 | 235,929 | yes | yes (`none`) | xhigh, medium, low, none | xhigh |
| `openai/gpt-5.6-terra` | 1,050,000 | 128,000 | yes | yes (`none`) | max, xhigh, high, medium, low, none | medium |
| `openai/gpt-5.6-sol` | 1,050,000 | 128,000 | yes | yes (`none`) | max, xhigh, high, medium, low, none | medium |
| `openai/gpt-5.6-luna` | 1,050,000 | 128,000 | yes | yes (`none`) | max, xhigh, high, medium, low, none | medium |

Prices (USD per million tokens in / out) [OR-models]: Sonnet 5.5 2 / 10; Opus 5.5 4 / 20; Haiku 5.5 0.10 / 0.50; DeepSeek 0.1165 / 4.66; GLM 0.0699 / 1.40; Qwen 0.15 / 1.875; Terra 2 / 12; Sol 4 / 20; Luna 0.20 / 1.20.

Observations:

- **All nine have a non-empty `supported_efforts`.** None is token-budget-only or on/off-only. `supports_max_tokens` (the flag [OR-docs] says marks token-budget models) is absent on all nine [OR-models]. So the "no effort levels" case does not occur in this list today; section 6 covers what to do if it does (for example after a list change).
- **`default_enabled`** is present only on GLM, Qwen and the three GPT-5.6 models, all `true` [OR-models]. For the others, reasoning being on by default is not stated by OpenRouter; vendors say it is (below).
- **The level sets differ by model**: Qwen has no `high`; GLM has only max/high/low; DeepSeek has no medium/xhigh. A single shared list of levels would offer values some models reject. Read levels per model [OR-docs: "Filter effort selectors to these values"].
- **Context windows are all about 1M**, so context is not a differentiator. Maximum output differs a lot: Qwen caps at 235,929 and the Claude and GPT-5.6 models at 128,000, which matters because reasoning shares the output limit (section 5).
- **The window is OpenRouter's, not always the vendor's native one.** [Qwen] says the native context is 262,144, "extensible up to 1,000,000" with YaRN scaling; OpenRouter advertises 1,024,000, so the hosting provider is serving the extended window. [OR-models] has no per-model detail on which. Not checked for the others.

## 2. What OpenRouter does with `effort` (recap and additions)

- Request shape: `"reasoning": {"effort": "<level>"}`. `effort` and `max_tokens` are alternatives, not both [OR-docs].
- Levels documented: max, xhigh, high, medium, low, minimal, none. As a share of the output limit: max/xhigh about 95%, high 80%, medium 50%, low 20%, minimal 10% [OR-docs]. That table is OpenRouter's description of how it maps effort for models that only take a token budget; vendors may treat effort as a behavioural signal instead (next point). Do not read it as a guaranteed token count.
- Anthropic's own description: "Effort is a behavioral signal, not a strict token budget" and it applies to all output tokens, "text responses", "tool calls and function arguments" and thinking [Anthropic]. So on Claude, low effort also means fewer and terser tool calls, which is relevant to an agent loop.
- `mandatory: true` means hide the disable control and never send `effort: "none"`, because the model rejects it [OR-docs].
- `default_effort: "none"` would mean "off by default" rather than a level to pre-select; none of the nine has that [OR-docs, OR-models].
- Omitting `reasoning` entirely leaves the model's own default in force (see conflict below). There is no OpenRouter-level override [OR-docs: "`default_enabled`: default on/off state when the user has not set `reasoning.enabled`"].

## 3. Per-model detail, with vendor cross-check

### Claude Sonnet 5.5, Opus 5.5, Haiku 5.5

- **Levels**: vendor supports all five (low, medium, high, xhigh, max) on all three [Anthropic]. OpenRouter matches, plus `none` for Haiku [OR-models].
- **Defaults and a conflict**: [Anthropic] says the default is `high` except Opus 5.5 and Haiku 5.5, which default to `medium`; omitting `effort` is identical to sending the default. [OR-models] reports `high` for Opus 5.5, which disagrees with the vendor. Cause not established (OpenRouter's metadata may be stale, or OpenRouter may inject `high`). Practical consequence: if the app omits `effort` on Opus 5.5, Anthropic says it runs at medium regardless of what the OpenRouter metadata says. To avoid depending on either, the app can send an explicit effort and pre-select what it sends. Needs a live test call to resolve; not done here.
- **Turning reasoning off**:
  - Sonnet 5.5: OpenRouter says mandatory [OR-models]. [Anthropic] agrees that `thinking: {"type":"disabled"}` is not offered; the lowest setting is `thinking: {"type":"between_tools"}` (no up-front thinking, allowed at low/medium/high effort, 400 at xhigh/max). OpenRouter's docs do not describe `between_tools`, so it is probably not reachable through the unified `reasoning` object [not verified].
  - Opus 5.5: "Adaptive thinking is always on and can't be turned off"; `disabled` returns 400 at every effort [Anthropic]. Matches `mandatory: true`.
  - Haiku 5.5: `disabled` is allowed at high effort or below and 400s at xhigh/max [Anthropic]. OpenRouter lists `none` with no such restriction [OR-models], so `none` combined with xhigh/max cannot occur (they are alternatives in one field). Thinking is on by default [Anthropic].
- **`max_tokens`**: "Thinking counts toward `max_tokens` even when the thinking content isn't returned" (Sonnet 5.5) and it is "a hard limit on total output (thinking plus response text)" (Opus 5.5) [Anthropic]. Anthropic recommends large limits at higher effort (64k to 128k for agentic work) [Anthropic]. OpenRouter adds that for Claude the output limit must exceed the reasoning budget [OR-docs, via the earlier note].
- **Per-message effort change** (cache-preserving) exists for all three, in beta [Anthropic]; OpenRouter documents it for some models and says support is per model [OR-docs]. Out of scope here.

### DeepSeek V4 Pro 0813

- **Levels**: vendor accepts `low`, `high`, `max`; other values are remapped: minimal to low, low to low, medium to high, high to high, xhigh to high, max to max [DeepSeek]. OpenRouter lists max, high, low, none [OR-models]; that is exactly the vendor's real levels plus off, so it does not offer the ones that would silently collapse.
- **Off**: thinking is on by default at `high`. Vendor disables via `thinking: {"type":"disabled"}` (OpenAI format) or `reasoning.effort: "none"` (Anthropic format) [DeepSeek]. OpenRouter exposes `none` [OR-models].
- **Other effects**: in thinking mode `temperature`, `presence_penalty` and `frequency_penalty` are accepted but have no effect [DeepSeek].
- **Tool calls**: with `tools` in the request, `reasoning_content` from earlier turns must be passed back in full on every later request, otherwise the API returns 400 [DeepSeek]. Likely OpenRouter handles this through its reasoning-preservation mechanism (`reasoning_details`) [OR-docs], but whether the app's message history round-trips it is a code question for the implementer, because the app currently ignores reasoning deltas (earlier note). **This is the biggest practical risk for DeepSeek in an agent loop; test it before listing the model.**

### GLM 5.3

- **Levels**: [Z.ai] describes no effort parameter at all; thinking is on/off only, and "GLM-5.3 and GLM-5.3-FLASH ... use forced thinking and cannot be disabled." OpenRouter marks it mandatory [OR-models] (consistent) but still advertises max/high/low with default `max`. How OpenRouter turns those levels into something the provider understands is not documented in the sources read; treat the GLM effort control as **unverified** (may have little or no effect) until tested with a measured `reasoning_tokens` comparison.
- **Tools**: interleaved thinking (reasoning between tool calls) is on by default; thinking blocks should be returned with tool results [Z.ai]. Preserved thinking across turns defaults off on the standard endpoint (`clear_thinking`) [Z.ai].
- Billing of reasoning tokens is not stated by [Z.ai].

### Qwen3.8 27B

- **Levels**: [Qwen] defines `reasoning_effort` with `xhigh` (default), `medium`, `low`, matching OpenRouter's xhigh/medium/low [OR-models]. Off: `enable_thinking: false` via `chat_template_kwargs` (or directly on Qwen Cloud), exposed on OpenRouter as `none` [OR-models].
- Thinking is on by default and "can be disabled per request" [Qwen]. The card notes that lower effort does not always reduce total task time in multi-turn agentic work [Qwen].
- Recommended thinking-mode sampling: temperature 1.0, top_p 0.95, top_k 20 [Qwen]; matches OpenRouter's `default_parameters` [OR-models].
- Tool-calling is listed by OpenRouter [OR-models]; the model card does not detail it.
- Max output on OpenRouter is 235,929 [OR-models]; the card suggests 262,144 for reasoning content and 131,072 for the final reply [Qwen]. At the top effort a long reasoning run can therefore hit the output cap.
- OpenRouter also notes some Qwen thinking models take `reasoning.max_tokens` (as `thinking_budget`) [OR-docs], but this model does not advertise `supports_max_tokens` [OR-models].

### GPT-5.6 Terra, Sol, Luna

- **Levels**: OpenAI's page says supported values are model-dependent and does not enumerate GPT-5.6's [OpenAI]. OpenRouter lists max through none for all three [OR-models]. For GPT-5.6, "if effort is omitted ... medium" [OpenAI], matching `default_effort: medium`.
- **Off**: `none` is accepted by OpenRouter [OR-models]. [OpenAI] says some newer models (GPT-6 Astra, GPT-6.1 Sol) reject `none`; that does not apply to the three here per [OR-models], but it shows the flag differs by model, so keep reading `mandatory`.
- **Reasoning content is not returned** by default on OpenAI models: they emit encrypted or summarised reasoning only [OR-docs: "some (like the OpenAI o-series) do not"; OpenAI: reasoning tokens are "not visible through the API"]. Billing and counting still apply.
- **Reasoning mode**: `reasoning.mode: "pro"` (OpenAI/Azure only) reroutes to a pro variant, same per-token rate, "typically consumes more tokens" [OR-docs, OpenAI]. Independent of effort. Not needed for the Effort control.
- OpenAI's own guide recommends `gpt-5.6-terra` for lower cost and `gpt-5.6-luna` for lowest cost and latency [OpenAI]; Sol is the flagship [OR-models description].

## 4. What Effort does to cost, latency and `completion_tokens`

Plainly:

- **More effort means more thinking tokens before the answer.** Thinking tokens are billed as output tokens at the completion price [OR-docs; OpenAI: "billed as output tokens"; Anthropic: effort affects thinking, text and tool calls]. So higher effort raises cost and latency (the model produces more tokens before the first visible token and in total). It does not change `prompt_tokens`.
- **`completion_tokens` includes reasoning.** The app's Trace reads `completion_tokens` and `cost` from the usage chunk; reasoning tokens are a part of `completion_tokens`, broken out separately as `completion_tokens_details.reasoning_tokens` [OR-docs, and the earlier note]. So the Trace will show the jump automatically, but cannot show how much of it was thinking unless it also reads `reasoning_tokens` (it does not today).
- **Hidden or redacted reasoning is still billed and counted.** `exclude: true` omits reasoning from the response but "the tokens are still billed" [OR-docs]. OpenAI models do not return reasoning text at all; Anthropic may return encrypted or `[REDACTED]` blocks in streaming [OR-docs]. In every case `completion_tokens` reflects the real total, which can far exceed the visible reply. A user cannot infer reasoning size from the answer length.
- **Output cut-offs.** On most providers reasoning shares `max_tokens`/`max_output_tokens` with the visible reply. If the model spends the whole limit thinking, Chat Completions returns `finish_reason: "length"` with empty `content`, and the reasoning is still billed [OR-docs; OpenAI: `status: incomplete`, `max_output_tokens`, "before any visible output"]. Detect it by `completion_tokens - reasoning_tokens` being 0 or near 0 [OR-docs]. Highest-effort settings on the models with the smallest output caps (Claude and GPT-5.6 at 128,000; Qwen at 235,929) are where this is likeliest. OpenAI suggests reserving at least 25,000 tokens for reasoning plus output at the start [OpenAI]; Anthropic suggests 64k to 128k for agentic work at xhigh/max [Anthropic].
- **Effort is a signal, not a quota.** Anthropic: at low effort Claude "still thinks on sufficiently difficult problems"; in tool loops, follow-up requests that only process tool results "can still skip thinking at any level" [Anthropic]. Qwen: lower effort does not always shorten multi-turn agent work [Qwen]. Expect variable, not proportional, savings.
- **Cache**: changing request-level effort between turns changes the request prefix and invalidates the prompt cache [OR-docs]. Per-message effort updates avoid that on supporting models. Relevant if the UI lets the user change Effort mid-conversation.
- **Not measured here:** the actual token and latency multipliers between levels per model. Sources give descriptions, not numbers. A short live experiment (same prompt, each level, read `reasoning_tokens` and `cost`) would give them.

## 5. What the app currently does with this

From the earlier note and `src/flood_agent/llm.py`: usage is read from the final chunk (`prompt_tokens`, `completion_tokens`, `cost`); `choices[].delta` is read for `content` and `tool_calls` only, so streamed `reasoning` / `reasoning_details` deltas are ignored harmlessly. Two things to keep in mind when Effort is added:

1. For DeepSeek (and, per [Z.ai], GLM) tool loops, reasoning content must be echoed back in later requests; ignoring it may cause 400s or degraded tool use. [DeepSeek] states 400 explicitly; for Claude [OR-docs] says preserving reasoning blocks matters during tool use.
2. A `finish_reason` of `length` with empty content is a new realistic failure at high effort; the Trace should be able to show it.

## 6. What the Effort control should show

Rule, from [OR-docs] "Use this when building client UIs", applied to the nine models:

| Model data | Control |
|---|---|
| `supported_efforts` non-empty | A selector with exactly those levels, in the order given. Pre-select `default_effort` (unless it is `none`). |
| plus `none` in the list and `mandatory` false | Include an "Off" choice (send `effort: "none"`). |
| `mandatory: true` | No "Off" choice; do not send `none`. Sonnet 5.5, Opus 5.5 and GLM 5.3 are in this group. |
| `supported_efforts` omitted, `supports_max_tokens: true` | A token-budget input (send `reasoning.max_tokens`), not an effort selector. |
| `supported_efforts` omitted, no `supports_max_tokens`, not mandatory, `reasoning` supported | A plain on/off toggle (`reasoning.enabled`), default per `default_enabled`. |
| `supported_efforts` omitted, mandatory | No control; show a read-only "reasoning always on" note. |
| no `reasoning` object | No control; "does not reason". |
| `supported_efforts: null` | All gateway levels are accepted [OR-docs]; offer the full documented set. |

None of the nine currently needs the last five rows, so the rule is future-proofing. Caveats for the first row: GLM 5.3's effort levels are not backed by vendor docs (section 3), and Opus 5.5's pre-selected default conflicts between sources. For both, either test with a live call or have the app send an explicit effort rather than relying on the default.

## 7. Open items

- Resolve the Opus 5.5 default conflict (`high` per OpenRouter vs `medium` per Anthropic) with a live request comparing `reasoning_tokens` with and without explicit effort.
- Measure whether GLM 5.3 effort changes anything.
- Verify DeepSeek (and GLM) tool-loop behaviour when reasoning is not echoed back.
- Check whether OpenRouter can reach Anthropic's `between_tools` setting (Sonnet 5.5's lowest thinking) through the unified `reasoning` object.
- Measure per-level token, cost and latency multipliers on the chosen models.
- Re-query `/api/v1/models` before hard-coding any level list; this snapshot will drift.
