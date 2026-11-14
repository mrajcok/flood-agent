# The system prompt is kept cacheable

Every LLM call resends the whole AI Cell context (the Cells above it, then its own prompt), and the provider only bills the cached part of that request at a fraction of the input price when the request starts with exactly the same text as an earlier one. The system prompt is the start of every request, so anything in it that changes between Runs makes every later token miss the cache. We therefore keep the system prompt a pure function of things that change rarely: the fixed instructions, then the Semantic Layer, then the live Result Tables last (those of the Cells above, current Runs only, plus the Cell's own earlier in its current Run, ordered by creation time), so a new Result Table only changes the end of it. The fixed instructions include one paragraph on the labelled messages (notes and queries the user ran); it never changes between Runs. Nothing that changes on its own goes in it, above all the current time. Instead the LLM writes relative time ranges with the SQL function `now()` and never needs to be told the time (we briefly gave it the time to the minute, and a prompt that changes every minute misses the cache for every Notebook, every minute).

## Considered Options

- Tell the LLM the current time in the system prompt, so it could write absolute timestamps. Rejected: it changes the prompt every minute. The same applies to any clock, counter or "last refreshed" text.
- Put the current time in the latest user message instead. Rejected for now: it keeps the system prompt stable, but the LLM would still write absolute timestamps into SQL that cannot be refreshed later (ADR 0006). `now()` also makes a stored query mean the same thing each time it runs.

## Consequences

- Relative time ranges are always `now()` in the LLM's SQL (documented in the Semantic Layer's `functions:`), so a refreshed table or count picks up the current time with no help from the LLM.
- The AI Cell context is built from what each Cell ran, never from Drafts, so it comes out byte for byte the same on every Run: each non-AI Cell is its own labelled user message (`[Markdown note]`, `[SQL Cell, run by the user]`, `[SHOW Cell, run by the user]`), and its Query Metadata is frozen at its Run, so it does not follow renames or refreshes. Editing a Cell in the middle of a Notebook breaks the cache for the AI Cells below it on their next Run, which is accepted.
- Changes that must not alter the system prompt need tests that compare it byte for byte. A Refresh keeps `created_at` fixed and counts "as of" and Retention from a separate `refreshed_at`, because the Result Tables section is ordered by `created_at` and a reorder would break the cache for the Cells below.
- When adding to the system prompt, ask whether the text can change between two Runs of the same Cell. If it can, it belongs at the end (as the Result Tables section does) or somewhere other than the system prompt.
- Which models cache without being asked, and which need `cache_control`, is in `docs/research/prompt-caching-per-model.md`; the app sends the fields each model needs.
