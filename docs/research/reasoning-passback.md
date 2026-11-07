# How OpenRouter wants reasoning passed back in tool loops

Research for issue #52 (map #45). Researched 2026-11-06. Builds on `docs/research/openrouter-model-controls.md` (#46: how to send `reasoning`, usage chunk shape) and `docs/research/model-reasoning-and-effort.md` (#50: the nine curated models, effort levels, cost of effort). Neither is repeated. Findings only; the decision to implement belongs to the ticket this blocks (#53).

## Sources and labels

Each fact is tagged with the source that owns it.

- **[OR-reasoning]** OpenRouter docs, [Reasoning Tokens](https://openrouter.ai/docs/use-cases/reasoning-tokens) (fetched as raw markdown; sections "Preserving Reasoning", "Reasoning Details API Shape", "Reasoning Context Mode").
- **[OR-tools]** OpenRouter docs, [Tool Calling](https://openrouter.ai/docs/guides/features/tool-calling), [OR-stream] [Streaming](https://openrouter.ai/docs/api-reference/streaming).
- **[OR-mig]** OpenRouter model migration guides (Claude Fable 5.1, GPT-5.6) under `/docs/cookbook/evaluate-and-optimize/model-migrations/`.
- **[OR-models]** live `GET https://openrouter.ai/api/v1/models`, fetched 2026-11-06 (458 entries). All nine curated models still list `tools`, `reasoning`, `include_reasoning`, `reasoning_effort` in `supported_parameters`, with the same `reasoning` objects as in #50.
- **[Anthropic]** Claude API docs: [Thinking](https://platform.claude.com/docs/en/build-with-claude/thinking) (sections "Thinking with tool use", "Preserving thinking blocks", "Thinking block preservation by model", "Thinking encryption", "Redacted thinking blocks", "Thinking and the context window") and [Thinking in tool and multi-turn workflows](https://platform.claude.com/docs/en/build-with-claude/thinking-tool-workflows).
- **[DeepSeek]** [Thinking mode](https://api-docs.deepseek.com/guides/thinking_mode).
- **[Z.ai]** [Thinking mode](https://docs.z.ai/guides/capabilities/thinking-mode).
- **[Qwen]** [Qwen3.8-27B model card](https://huggingface.co/Qwen/Qwen3.8-27B).
- **[OpenAI]** [Reasoning guide](https://developers.openai.com/api/docs/guides/reasoning).
- **[probe]** my own live calls to `POST /api/v1/chat/completions` on 2026-11-06 with this account's key (about 30 tiny requests, a few cents). Method in section 8. These are single runs, so treat them as observations, not guarantees.

Vendor pages other than Anthropic's were read through a summarising fetch tool; quotes are close, not verbatim. OpenRouter's and Anthropic's pages were read as raw text.

## 1. Short answer

1. **Stream side.** OpenRouter streams reasoning in `choices[].delta.reasoning_details` (array of typed objects) and, for text reasoning, *also* in `choices[].delta.reasoning` (plain string). The same text arrives in both. [OR-reasoning, probe]
2. **Send-back side.** Put it on the `assistant` message in the next request as `reasoning_details` (the full array) or, for plain-text reasoning, as `reasoning` (string). `reasoning_content` is documented as an alias of `reasoning`. [OR-reasoning] Only `reasoning_details` is the complete form. `reasoning_content` is also what DeepSeek, Z.ai and Qwen use natively, but OpenRouter is the one we talk to.
3. **Which to store.** Store `reasoning_details` verbatim. It is the only form that works for every model. In the probe, Claude only got its thinking back from `reasoning_details`; the string forms did nothing for Claude (section 5). For DeepSeek, GLM and Qwen all three forms behaved the same.
4. **What happens without it.** In the probe, nothing failed on any model, including DeepSeek, whose vendor docs promise HTTP 400. The model just re-thinks (more reasoning tokens, possibly a different decision). So OpenRouter turns "error" into "degraded", at least on the providers this account is allowed to reach. Do not rely on that for DeepSeek (section 4).
5. **Scope.** Required within the current turn's tool loop for Claude and DeepSeek (vendor docs). Useful across turns for Claude on our three models (they keep all prior thinking), required across turns for DeepSeek when `tools` is sent (vendor), optional for Qwen (default keeps), and a no-op by default for GLM on the standard endpoint (section 6).
6. **Cost.** Passed-back reasoning is counted in `prompt_tokens` on every later request, on top of being billed once as output. The probe confirmed it to within a few tokens (section 5).

## 2. Wire shapes (exact)

### Streaming chunk

From [OR-reasoning] "Reasoning Details API Shape", confirmed by [probe]:

```json
{"choices":[{"delta":{
  "reasoning": "The user wants weather",
  "reasoning_details":[{"type":"reasoning.text","text":"The user wants weather","format":"unknown","index":0}]
}}]}
```

- `reasoning_details` is an array of objects with common fields `id` (string or null), `format`, optional `index`. [OR-reasoning]
- Three `type`s [OR-reasoning]:
  - `reasoning.summary`: `{"type","summary","id","format","index"}`
  - `reasoning.encrypted`: `{"type","data","id","format","index"}`; "may be redacted or protected"
  - `reasoning.text`: `{"type","text","signature","id","format","index"}`, `signature` may be null.
- `format` values documented: `unknown`, `openai-responses-v1`, `azure-openai-responses-v1`, `bedrock-openai-responses-v1`, `bedrock-xai-responses-v1`, `xai-responses-v1`, `meta-responses-v1`, `openrouter-stealth-v1`, `anthropic-claude-v1` (default), `google-gemini-v1`. [OR-reasoning] It records which provider's dialect the block is in.
- **Streaming = fragments.** "Each reasoning detail chunk is sent as it becomes available ... The complete reasoning sequence is built by concatenating all chunks in order." [OR-reasoning] The docs do not say how to merge fragments into the final array. [probe] shows what actually comes:
  - Many tiny `reasoning.text` objects, all with the same `index` (0), `text` a few words each, `id` absent.
  - For Claude the **last** fragment has no `text`, only `"signature": "<~1,750 chars>"` and the same `index`. So a Claude thinking block is "all `text` fragments for an `index`, concatenated, plus the `signature` from the final one". Merging by `(type, index)` and concatenating `text`/`summary`/`data` and keeping `signature`/`id`/`format` reproduced a form OpenRouter accepted and honoured (section 5).
  - `format` was `anthropic-claude-v1` for Claude and `unknown` for DeepSeek, GLM and Qwen.
- For encrypted reasoning, "the content may appear as `[REDACTED]` in streaming responses". [OR-reasoning] Not seen in [probe].
- The non-streaming shape is the same objects in `choices[].message.reasoning_details`. [OR-reasoning]
- Some models give none: OpenAI o-series "do not return their reasoning tokens". [OR-reasoning] Claude Haiku/Sonnet/Opus 5.5 returned no `reasoning_details` at all for a trivial one-tool prompt (adaptive thinking skipped it, as [Anthropic] allows), but returned them for a prompt that needed planning. GLM 5.3 at `effort: low` returned none and 1 reasoning token; at its default effort it did. So "no reasoning in this step" is normal and the app must treat the field as optional per step.

### Request: what to send back

[OR-reasoning] "Preserving Reasoning" > "Chat Completions": "pass it back ... in one of two ways":

1. `message.reasoning` (string): "the plaintext reasoning as a string field on the assistant message".
2. `message.reasoning_details` (array): "the full reasoning_details block". "Use `reasoning_details` when working with models that return special reasoning types (such as encrypted or summarized) - this preserves the full structure needed for those models."

`reasoning_content` "can also be used as an alias - it functions identically to `reasoning`". [OR-reasoning]

Shape for a tool loop step (assistant message with `tool_calls`), per [OR-reasoning] and the assistant-message format already used by the app:

```json
{"role":"assistant","content":"","tool_calls":[{"id":"call_1","type":"function","function":{"name":"get_weather","arguments":"{\"city\":\"Paris\"}"}}],
 "reasoning_details":[{"type":"reasoning.text","text":"...","signature":"...","format":"anthropic-claude-v1","index":0}]}
```
followed by the `role: "tool"` messages. This is exactly what [probe] sent. The OpenRouter docs have no Chat Completions tool-loop example with `reasoning_details` in the page text read (the code samples are tabs that did not survive as markdown), so the exact placement above is confirmed by [probe], not by a quoted example.

### Ordering and integrity rules

- OpenRouter: "the entire sequence of consecutive reasoning blocks must match the outputs generated by the model during the original request; you cannot rearrange or modify the sequence of these blocks." [OR-reasoning]
- Anthropic: within the latest assistant message the consecutive `thinking` blocks "must match what the model generated ... you can't rearrange, edit, or partially drop them", including `redacted_thinking`; "Modified thinking blocks are rejected with a 400 error." [Anthropic]
- Z.ai: "All consecutive reasoning_content blocks must exactly match the original sequence ... Do not reorder or edit these blocks; otherwise, performance may degrade and cache hit rates may be affected." [Z.ai]
- Consequence: store the merged array exactly as built from the stream, in order, and never trim, summarise or edit it. Position in the message list matters too: the reasoning belongs with the assistant message that produced it, before that step's tool results.
- Claude Fable 5.1 additionally binds each block to the transcript prefix before it (edit history and the block is invalid), but "requests through OpenRouter are not subject to this enforcement". [OR-mig] Fable is not in our list; Sonnet/Opus/Haiku 5.5 are, and [Anthropic] says the prefix check "runs on Claude 5.1 and later models", so it does not apply to 5.5 as documented. Not tested.

## 3. Which candidate models need it

| Model | Needs it in the tool loop? | Evidence | What happens without it |
|---|---|---|---|
| Claude Sonnet / Opus / Haiku 5.5 | Yes, required by the vendor: "Required: within a tool-use turn, pass thinking blocks back." | [Anthropic] | Vendor/OpenRouter docs: rejected or degraded. [probe] through OpenRouter: no error; the model re-thought (Haiku 186 vs 79 reasoning tokens, Sonnet 40-54 vs 0). Degraded, not failed. |
| DeepSeek V4 Pro | Yes, required by the vendor when `tools` is sent: "must be fully passed back to the API in all subsequent requests". | [DeepSeek] | Vendor says "the API will return a 400 error". [probe] through OpenRouter: no 400, 16-20 re-thinking tokens instead of 1. |
| GLM 5.3 | Recommended: "thinking blocks should be explicitly preserved and returned together with the tool results". | [Z.ai] | Z.ai does not say what happens. [probe]: no error; prompt grew by the reasoning size when passed. |
| Qwen3.8 27B | Not stated as required; the model card shows history carrying `reasoning_content` and keeps prior thinking by default. | [Qwen] | [probe]: no error, no visible difference in 1 sample. |
| GPT-5.6 Terra / Sol / Luna | Recommended, on the Responses API: "highly recommend you pass back any reasoning items returned with the last function call". | [OpenAI] | Not specified; implied lower quality and token efficiency. **Not probed** (this account's allowed-providers setting blocks both providers that serve GPT-5.6, `404 No allowed providers`). |

Notes on GPT-5.6:

- [OpenAI] steers reasoning models to the Responses API ("work better with the Responses API") and does not say Chat Completions returns reasoning. [OR-reasoning] says OpenAI o-series models do not return reasoning text; GPT-5.6's behaviour on `/chat/completions` through OpenRouter is **unverified**. OpenRouter's block format list includes `openai-responses-v1`, which suggests encrypted/summary blocks can come back via `reasoning_details`, but that is inference.
- For GPT-5.6 the model sees all earlier turns' reasoning by default (`reasoning.context` defaults to `all_turns`) *if the items are in the request*; `reasoning.context` is the knob (`auto`, `all_turns`, `current_turn`). [OpenAI, OR-reasoning] This is the one model where OpenRouter offers a documented switch for "current turn only".

## 4. The DeepSeek 400: documented vs observed

[DeepSeek] is explicit that with `tools` in the request, omitting earlier `reasoning_content` gives HTTP 400, and that this holds across user turns too ("Its Turn 2 request still pass[es] the `reasoning_content` generated in Turn 1"). Without `tools`, it is ignored and need not be passed back.

[probe]: via OpenRouter, a DeepSeek V4 Pro tool loop with the assistant message carrying no reasoning completed normally (HTTP 200), both at default effort and at `low`. Likeliest reasons, none verified: OpenRouter fills in or tolerates the missing field; the provider that served the call (this account is limited to google-vertex, nebius, nvidia, venice, and the probe did not capture which one) is lenient; or the 400 applies only to DeepSeek's own endpoint. So: the app must not assume this is safe. A different provider route, or a later DeepSeek change, could return the 400. Passing `reasoning_details` back is cheap insurance, and the vendor's own wording is "all subsequent requests".

## 5. Measured effect of passing back (single runs)

Each row: a 2-step tool loop (user prompt, assistant with one or more tool calls, tool result), step 2 sent four ways. `prompt_tokens` is from the final usage chunk of step 2 (all variants had the same prefix otherwise, so the difference is the reasoning). "Step-1 reasoning" is `completion_tokens_details.reasoning_tokens` of step 1.

| Model | Step-1 reasoning tokens | Step-2 prompt_tokens: none / `reasoning` / `reasoning_content` / `reasoning_details` | Step-2 reasoning tokens: none / details |
|---|---|---|---|
| deepseek-v4-pro-0813 (default effort) | 20 | 357 / 376 / 376 / 376 | 16 / 1 |
| deepseek-v4-pro-0813 (`low`) | 17 | 357 / 373 / 373 / 373 | 15 / 1 |
| glm-5.3 (default effort) | 32 | 192 / 223 / 223 / 223 | 25 / 23 |
| qwen3.8-27b (default effort) | 29 | 625 / 653 / 653 / 653 | 29 / 23 |
| claude-haiku-5.5 (`high`) | 41 | 499 / 499 / 499 / **541** | 186 / 79 |
| claude-sonnet-5.5 (`high`) | 297 | 762 / 762 / 762 / **1059** | 40 / 0 |

Reading it:

- **Reasoning passed back adds about its own token count to `prompt_tokens`** (Sonnet: 1059 - 762 = 297 = step-1 reasoning tokens exactly; Haiku +42 vs 41; DeepSeek +19 vs 20; GLM +31 vs 32; Qwen +28 vs 29). So it is input-billed on every later request, in addition to being output-billed when generated. [probe; matches Anthropic: prior thinking "billed as input tokens", "Thinking and the context window"]
- **For DeepSeek, GLM and Qwen the three field names are equivalent**: identical `prompt_tokens`. A `reasoning` string is enough for those.
- **For Claude only `reasoning_details` works.** `reasoning` and `reasoning_content` added nothing to `prompt_tokens`; a plain string cannot carry the `signature`, so OpenRouter dropped it. The model re-thought. A design that stores only the `reasoning` string will silently do nothing for Claude, the current default model.
- Without passing back, models re-think after each tool result, which costs output tokens.
- Anthropic's side: reasoning blocks are cached with tool results and "count as input tokens ... when read from the cache" ([Anthropic] "Thinking and prompt caching"). Changing thinking configuration or effort invalidates the cache (section 9).

## 6. Within the turn's tool loop, or across turns?

| Model | In-turn loop | Across user turns |
|---|---|---|
| Claude Sonnet/Opus/Haiku 5.5 | Required. | Allowed to omit outside tool use; "Recommended: across turns, pass everything back". These three are "keep all prior turns" models, so old thinking stays in context and is billed as input; the API filters and "bills input tokens only for the blocks actually shown to Claude". Long sessions accumulate thinking. [Anthropic] |
| DeepSeek V4 | Required with `tools`. | Also required with `tools`, even for turns without a tool call. Not required without `tools`. [DeepSeek] |
| GLM 5.3 | Recommended (interleaved thinking on by default). | "Preserved thinking" across turns defaults to **disabled** on the standard API endpoint (`clear_thinking`), enabled on the Coding Plan endpoint; to use it, set `thinking.clear_thinking: false` and return complete, unmodified `reasoning_content`. [Z.ai] OpenRouter's docs do not describe `clear_thinking`, so whether it can be set through the unified `reasoning` object is unverified; by default, old reasoning is simply not used. |
| Qwen3.8 27B | Not specified. | Keeps all historical thinking by default (`preserve_thinking`); `preserve_thinking: false` keeps only the latest user message's. [Qwen] Whether OpenRouter exposes that switch is not documented. |
| GPT-5.6 | Recommended (Responses API). | Rendered by default (`all_turns`) if present in the request; `reasoning.context: "current_turn"` ignores older items. Same model family only; other families' reasoning is omitted. [OpenAI, OR-reasoning] |

Practical reading: the safe, simple rule is "send back every assistant message's `reasoning_details` unchanged, for the whole stored conversation". That satisfies the in-turn requirement and the across-turn vendor rules; the providers drop or ignore what they do not use (Claude filters, GLM ignores by default). The cost is input tokens, bounded by what each provider chooses to keep. A cheaper variant, "current turn only" (strip `reasoning_details` from messages before the latest user message), is allowed for Claude (outside tool use) and for models without tools-required rules, but conflicts with the DeepSeek vendor rule and loses prefix-cache hits on Claude (the removed blocks change the cached prefix, per [Anthropic] "A thinking block the API drops ... changes the cached prefix from that block's position onward"). Not tested.

**Switching model mid-conversation.** A Claude thinking block is "readable only by the model that produced it and certain other models, and the API ignores or drops the blocks the target model can't read", and "keep passing thinking blocks back unchanged". [Anthropic] Among our three, Opus 5.5 reads Sonnet 5.5 and Haiku 5.5 blocks (Claude API) but Sonnet and Haiku do not read Opus 5.5's. [Anthropic] Blocks from Claude sent to DeepSeek and the reverse: not documented by anyone; `format` identifies the origin, and the OpenRouter docs do not say what a mismatched `format` does. Untested. The app has a model choice (curated list), so this will come up.

## 7. Encrypted and redacted reasoning

- OpenRouter: `reasoning.encrypted` objects carry `data`; "may be redacted or protected"; in streaming it "may appear as `[REDACTED]`". [OR-reasoning] These objects are part of the array and must be passed back unchanged, like the others (the sequence rule above).
- Anthropic: the full thinking is encrypted in the `signature`; `redacted_thinking` blocks hold opaque `data` when "portions of Claude's reasoning are safety-redacted"; pass back unchanged; "Filtering on `block.type == "thinking"` alone silently drops `redacted_thinking` blocks and breaks the multi-turn protocol" (and filtering them out of the latest message triggers a 400). [Anthropic] Display can be `omitted` (empty `thinking` text, `signature` only); on Opus 5.5 omitted is the default, but OpenRouter defaults Claude to summarized so the trace is not lost. [Anthropic, OR-reasoning] In both cases the block must still be echoed.
- OpenAI: reasoning items are opaque; in stateless mode the item carries `encrypted_content` "you can pass to future calls"; OpenRouter "automatically requests encrypted reasoning content" on the Responses API. [OpenAI, OR-reasoning] For Chat Completions via OpenRouter, the corresponding `format: openai-responses-v1` blocks in `reasoning_details` are the likely carrier; unverified.
- Practical rule for the app: treat every element of `reasoning_details` as opaque. Do not filter by `type`, do not decode `data` or `signature`, do not display `[REDACTED]` as if it were text, and never write one back with changed content.
- Signatures from Sonnet 5.5 and Haiku 5.5 "work only in the account that produced them or in an account linked to it". [Anthropic] Matters only if the SQLite store were ever shared across accounts/keys; stored blocks would then be dropped without an error.

## 8. How the probe was done

Script in the session scratchpad (not committed): streams `POST /api/v1/chat/completions` with `stream: true`, one `get_weather` tool, `max_tokens: 3000`, optional `reasoning: {"effort": ...}`. Step 1 parses `delta.content`, `delta.tool_calls`, `delta.reasoning`, `delta.reasoning_details` and the final usage chunk. Step 2 resends `[user, assistant(tool_calls + <variant>), tool]` with the variants "none", `reasoning` string, `reasoning_content` string, `reasoning_details` merged by `(type, index)`. Claude needed a prompt that required planning ("Think carefully first. I have 3 cities ...") because on a trivial prompt adaptive thinking produced no reasoning at all. Opus 5.5 was run only on the trivial prompt (no reasoning produced), so nothing is learned about it beyond the vendor docs. Model ids were the nine from #50; GPT-5.6 could not be reached (provider filter). The provider that served each request was not recorded. Cross-turn behaviour (reasoning from a previous user turn) and model switching were not probed.

## 9. What the app does today, and what would have to change

### Today

- `src/flood_agent/llm.py` `OpenRouterClient.stream_chat`: builds the body (no `reasoning` field yet) and reads each `delta` for `content` and `tool_calls` only; `delta.reasoning` and `delta.reasoning_details` are never read, so they are dropped. It yields `TextChunk`, then `ToolCallChunk`s after the stream ends, then `UsageChunk` (`prompt_tokens`, `completion_tokens`, `cost`).
- `src/flood_agent/agent.py` `_converse` (lines ~553-612): per step it builds `messages = [system] + conversation.messages`, consumes chunks, then constructs `assistant = {"role": "assistant", "content": text}` plus `tool_calls` if any. It saves `assistant` and the `tool` results together with `store.add_step(...)` and extends `conversation.messages` with the same dicts. So the dicts held in memory *are* what is sent back on the next step, and what `open_conversation` reloads (after `repair_history`) for the next turn. Error/give-up notices are saved as plain `{"role":"assistant","content":...}`.
- `src/flood_agent/store.py`: `messages(body TEXT)` holds `json.dumps(message)`; `messages()` returns `json.loads` of each body in id order; `transcript()` returns the same dicts to `web.py`, which reads only `role` and `content`. There is no column per field.

### Needed changes (no code written here)

1. **`llm.py` parsing.** Accumulate `delta.reasoning_details` across the whole stream and emit it once, e.g. a new `ReasoningChunk(details: list[dict])` in the `Chunk` union, yielded after the stream ends (next to the tool calls). Merge fragments by `(type, index)`: concatenate `text`, `summary` and `data`; keep the last non-null `signature`, `id` and `format`; keep first-seen order. Ignore `delta.reasoning` (same text, duplicated) unless a provider sends it without `reasoning_details`; in that case wrap it as a single `reasoning.text` object or keep it as a `reasoning` string field. The existing completeness checks need no change, and a `choices[0].delta` with only `reasoning_details` already falls through the `content`/`tool_calls` code harmlessly (today's behaviour).
2. **`agent.py` message building.** In `_converse`, take the `ReasoningChunk` (if any) and set `assistant["reasoning_details"] = details` when non-empty. Set it on every assistant message the LLM produced, including the final no-tool-call one of a turn (DeepSeek wants it on all later requests; Claude on keep-all models uses it across turns). Nothing else in the send path changes: `messages += conversation.messages` already forwards extra keys. The failure/notice messages stay as they are (no reasoning). `repair_history` copies dicts, so it preserves the field, and dropping an unanswered tool_calls message drops its reasoning with it, which is right.
3. **`store.py`.** No schema change is needed because `body` is JSON of the whole message. Size grows: a Claude `signature` is about 1.7 KB per step in the probe, plus the reasoning text; large but ordinary for SQLite TEXT. `delete_turn`/`delete_conversation` already remove messages by turn so reasoning is removed with them. `web.py` ignores unknown message keys. Check any code that iterates message keys (none found in `src`).
4. **Request body.** Passing reasoning back works whether or not `reasoning.effort` is sent, but note `exclude: true` (or `include_reasoning: false`) omits reasoning from responses so nothing could be stored; do not use it with a tool loop on these models. [OR-reasoning] "still billed" applies.
5. **Models without `reasoning_details`.** Treat the field as optional per step (Claude skips thinking on simple steps; GLM at `low` returned none; OpenAI may return none on Chat Completions). An assistant message with no `reasoning_details` is valid.
6. **Model switch.** Decide what to do with stored blocks when the user changes model. Options: send as-is (Anthropic says blocks the target cannot read are dropped without error; behaviour across vendors undocumented), or strip `reasoning_details` whose `format` does not match the new model's family. Needs a test before choosing; neither is verified here.
7. **Trace and cost visibility.** `UsageChunk` carries `prompt_tokens`, `completion_tokens` and `cost`. Passed-back reasoning raises `prompt_tokens` across a session, and the Trace already shows `prompt_tokens`. To explain the growth, also read `usage.completion_tokens_details.reasoning_tokens` (documented, present in the probe) and optionally store a short reasoning text in the Trace's `raw_response`. Not required for correctness.
8. **Tests.** `fake_llm.py` and the stream-parsing tests need a scripted `reasoning_details` stream, and a test that the saved assistant message round-trips into the next `stream_chat` call unchanged (field present, array order preserved).
9. **Cache and effort.** Changing `reasoning.effort` (or thinking config) between requests changes the cached prefix; if effort can be changed mid-conversation, expect a cache miss on the next request. [OR-reasoning, Anthropic] Per-message effort (`configuration_update`) exists for some models only.

### Things this does not decide

- Whether to also show the reasoning to the user (a UI/Trace question; the data needed is the same array).
- Whether to prune old reasoning to bound `prompt_tokens`; Anthropic's API already filters for its models and offers `clear_thinking_20251015` context editing, but OpenRouter's passthrough of that is undocumented. Measure on a long conversation first.

## 10. Open items

- Capture which provider served DeepSeek/GLM/Qwen calls (`provider` field in the stream chunks) and test other providers to see whether the DeepSeek 400 appears.
- Probe GPT-5.6 once the account's provider filter allows `azure` or `openai`: does `/chat/completions` return `reasoning_details` (format `openai-responses-v1`) and does passing it back change `prompt_tokens`?
- Probe Opus 5.5 on a prompt that makes it think, and the effect of editing/dropping a Claude block (expect 400 per [Anthropic]; through OpenRouter unknown).
- Probe cross-turn (reasoning from an earlier user turn) and model switches (Claude to DeepSeek and back) for errors.
- Check whether `thinking.clear_thinking: false` (GLM) or `preserve_thinking` (Qwen) can be sent via OpenRouter; no OpenRouter doc found for either.
- Re-query `/api/v1/models` before hard-coding anything; this is a 2026-11-06 snapshot.
