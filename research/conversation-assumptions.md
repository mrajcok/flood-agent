# What outside the web page assumes a Conversation or a turn?

Research for [#101](https://github.com/mrajcok/flood-agent/issues/101), part of map [#92](https://github.com/mrajcok/flood-agent/issues/92) (replace Conversations with Notebooks of Markdown, SQL and AI Cells).

**Sources**: the repo itself at `db79b5a` (main). All citations are `path:line` in that commit. The Notebook model is taken from the resolutions of #93 (what an AI Cell sees), #94 (re-runs, ownership, stale), #95 (settings chip), #97 (SQL Cell) and #100 (Command drop-down / SHOW / DESCRIBE).

**Scope**: Python code and data outside `templates/` and `static/`. The web routes in `web.py` are mentioned only where they hold logic (context meter, run lifecycle).

Each section has three parts:
- **Assumes**: what the code relies on today.
- **Mechanical**: renames and re-plumbing whose outcome is already settled by #93–#100.
- **Decide**: questions the spec must answer. They are collected again at the end.

---

## 0. The core: the Agent's turn loop and the message history

This is not one of the listed areas, but every listed area hangs off it.

**Assumes**
- A `Conversation` is an id plus one flat, append-only list of LLM messages (`src/flood_agent/agent.py:84-89`). `open_conversation` loads *every* stored message of the Conversation in id order (`agent.py:371-375`, `store.py:219-223`) and `repair_history` cleans the whole list (`agent.py:238-260`).
- Each LLM step sends `[system] + conversation.messages` (`agent.py:624-626`) and appends that step's messages to the in-memory list (`agent.py:684`). The "history" of the next turn is therefore exactly the previous request plus its reply, which is what makes prompt caching work across turns (ADR 0007).
- A turn is created lazily, together with its Conversation (`agent.py:566-568`; `web.py:343-347`), and is identified by an autoincrement `turns.id` (`store.py:34-40`, `store.py:151-156`).
- A stopped turn and a failed turn both write an assistant message into the history (`agent.py:593`, `agent.py:605`), so "the next prompt sees it" (GLOSSARY, Trace).
- Per-turn limits are named "per user turn" (`config.py:73-78`, `defaults.toml:17-18`).
- Nothing stops two turns from running in one Conversation at once. Turns are keyed by a random `turn_key` (`agent.py:518-523`, `web.py:344`), not by Conversation.

**Mechanical**
- `run_turn(conversation, user_message, max_result_rows, model, effort)` (`agent.py:502-505`) becomes "run AI Cell": the model, effort and rows come from the cell's stored settings (#95).
- `conversation.messages` is replaced by an **AI Cell context**, assembled when the cell runs from the latest Runs of the cells above (#93, #94). The cell's own Run messages are appended after it during the run, as they are today.
- `repair_history` is applied to each Run's stored messages (or to the assembled context). The algorithm is unchanged.
- "Per user turn" limits become "per AI Cell Run".
- "One cell runs at a time per notebook" (map) needs a per-notebook guard in the Agent. Today no such guard exists.

**Decide**
- **D1. What is the wire shape of a SQL Cell, a SHOW/DESCRIBE Cell and a ticked Markdown Cell in the AI Cell context?** Options are a `user` message (the wording of #93), or a synthetic assistant `tool_call` plus `tool` result pair. The choice ripples into Fake Mode (§3, which only reads `tool` results), flagging (§7, which accepts both roles) and providers that dislike consecutive `user` messages. This is one decision, but it is what makes D4, D5 and D11 concrete.
- **D2. Do a stopped AI Cell Run and a failed AI Cell Run count as "ran" for the cells below?** Today the stop note and the error message enter the history (`agent.py:585-607`), so the next turn sees them. #93 only says that "not run" cells are left out.
- **D3. Does "one cell runs at a time" also cover SQL Cells, and how is a running SQL Cell stopped?** SQL Cells run through `queries.run_query` in a worker thread, as Refresh does. That thread is bounded only by the query timeout and cannot be cancelled today (`agent.py:474-492` for counts; the table Refresh path is the same).

---

## 1. Eval harness (`src/flood_agent/evalset/`)

**Assumes**
- Each golden question runs as **one turn in a fresh Conversation** with its own app and results databases (`evalset/runner.py:108-126`). `Conversation()` is imported from `agent` (`runner.py:29`).
- The judge collects "the Result Tables the LLM created in the turn" with `agent.tables.tables_of(conversation.id)` (`runner.py:141`). It gets counts from the turn's Trace (`runner.py:66-78`).
- `smoke_caching.py` runs **one turn** and checks that step 2's request starts with step 1's (`evalset/smoke_caching.py:1-5`, `48-56`, `173-198`). It never checks caching *across* turns.
- `golden.yaml` questions are all single-shot (`evalset/golden.yaml:1-8`). None depends on an earlier answer.
- `client.py` and `compare.py` assume nothing about Conversations. `UsageMeter` only passes `session_id` through (`client.py:20-21`).

**Mechanical**
- `Conversation()` + `run_turn` becomes "a new Notebook with one AI Cell, run it". `tables_of(conversation.id)` becomes "tables of that cell's current Run".
- `smoke_caching.run_turn` changes the same way.

**Decide**
- **D4. Should the golden set gain multi-cell cases**, e.g. a SQL Cell, Markdown Cell or SHOW Cell above an AI Cell question that depends on it, so the harness measures whether models use the new context shape (D1)? Today the harness cannot express a question with anything above it.
- **D5. Should the caching smoke check also check across cells**, i.e. that AI Cell 2's first request starts with AI Cell 1's last request? #93 assumes "running top to bottom keeps adding to the same prefix", but nothing verifies it. It holds only if the assembled context reproduces earlier requests byte for byte (see D9, D10).

---

## 2. MCP server and connection

**Assumes**
- The client sends the Conversation id in every tool call's `_meta` (`mcp_connection.py:9-10`, `mcp_connection.py:105-112`; key in `mcp_protocol.py:3-4`). The Agent passes `conversation.id` to `call_tool` (`agent.py:665-666`, `agent.py:724`).
- The **server no longer reads it**. It reads only the row cap (`mcp_server.py:13-16`, `mcp_server.py:91-96`), because ADR 0003 superseded the isolation part of ADR 0002.
- `META_MAX_RESULT_ROWS` is described as "the row cap the user chose for this turn" (`mcp_protocol.py:6-7`).
- LLM-facing text: `describe_table` says it "works for any live table from any chat" (`mcp_server.py:252-254`). Tool descriptions are part of every request's cached prefix.
- The server is one long-lived process shared by all Conversations (ADR 0002). This is independent of the Conversation concept.

**Mechanical**
- The row cap comes from the AI Cell's settings rather than "this turn's" picker. The wire format is the same.
- Reword "from any chat" to "from any notebook". This changes the tools' text once, which is a one-time cache miss.
- SQL Cells never touch MCP: they call `queries` directly, as Refresh does (#96, #97).
- The ADR 0002 and ADR 0003 wording changes when the work is built.

**Decide**
- **D6. Drop `flood_agent/conversation_id` from `_meta` altogether, or carry a notebook/cell id?** Nothing reads it (`mcp_server.py:13-16`), and `tests/test_meta_conversation_id.py` (4 tests) only checks that it is sent and is not a scope. Dropping it is the simpler option, but ADR 0002's `_meta` channel then carries only the row cap.

---

## 3. Fake Mode (`fake_llm.py`, `canned_prompts.yaml`)

**Assumes**
- The script is chosen by matching the **latest `user` message**, and the step is the number of assistant messages after it (`fake_llm.py:3-6`, `fake_llm.py:114-116`).
- **Follow-ups** (`after:`) require the parent Canned Prompt to have run *earlier in this Conversation*: `_scope` searches earlier `user` messages for the parent's prompt and raises `ParentNotRun` otherwise (`fake_llm.py:27-31`, `fake_llm.py:142-151`). The reply says "run *X* first in this Conversation" (`fake_llm.py:199-200`). There is one follow-up, `chart-top-destinations` after `top-destinations` (`canned_prompts.yaml:187-201`; the format is documented at `canned_prompts.yaml:8`).
- The `{result_table}`, `{result_table:-2}` and `{count}` placeholders resolve **only from `tool` results** of `run_query` / `run_count_query` in scope (`fake_llm.py:203-236`, `canned_prompts.yaml:21-23`). `if_previous_failed` looks at the turn's latest tool result (`fake_llm.py:53`, `fake_llm.py:194-196`).
- The picker indents follow-ups under their parent (`tests/test_fake_mode.py:281`). That is page work and out of scope here.

**Mechanical**
- "Earlier in this Conversation" becomes "in an AI Cell above". If the parent is a cell above in the assembled context, `_scope` keeps working unchanged, because the AI Cell's own prompt is still the last `user` message. The reply text changes to "…first in a cell above".
- If ticked Markdown Cells or SQL Cells become `user` messages (D1), the "latest user message" is still the AI Cell's prompt, so script selection is unaffected. A follow-up's parent search could, however, match a **Markdown Cell** whose text equals the parent prompt. That is harmless but odd.

**Decide**
- **D7. Should a follow-up's `{result_table}` resolve from a SQL Cell above as well as from the parent AI Cell?** E.g. "Now chart those bytes" below a SQL Cell. Today only `tool` results are read (`fake_llm.py:216-224`), so with D1 = "user message" a SQL Cell's table is invisible to Fake Mode.
- **D8. Do follow-ups keep their parent requirement, or become self-contained?** In a notebook the user can copy or move cells, so "the parent ran above" is checkable. But it is a new failure mode: the parent sits *below*, or it ran in an earlier Run whose tables are no longer current (#94).

---

## 4. System prompt (`semantic_layer.py`, ADR 0007)

**Assumes**
- The prompt is fixed instructions + Semantic Layer + "the Conversation's live Result Tables" last (`semantic_layer.py:102-109`; ADR 0007).
- The Result Tables section's **LLM-facing wording** says "None are available in this Conversation", "Earlier results in this Conversation" and "another Conversation" (`semantic_layer.py:112-126`). Each table line includes `from request: <user prompt>` (`semantic_layer.py:128-130`).
- The table list is recomputed **on every LLM step** from `tables_of(conversation.id)` (`agent.py:624`). A step therefore also sees the tables its own turn created in earlier steps. Ordering is by `created_at` (`catalog.py:260-266`), and Refresh keeps `created_at` fixed so as not to reorder (ADR 0007, Consequences).
- `SYSTEM_INSTRUCTIONS` tells the LLM that values come from "the user's messages, the Semantic Layer or Query Metadata, or a run_count_query count" (`semantic_layer.py:50-55`). It has no notion of SQL Cells, Markdown Cells or SHOW rows.
- Byte-identity is tested: `tests/test_refresh_table.py:212`.

**Mechanical**
- `tables_of(conversation_id)` becomes "live tables of the current Runs of the cells above, by `created_at`" (#93, #94). Earlier-Run tables are excluded (#94).
- Reword the Conversation sentences to cells and notebooks. This is static text, so it stays cacheable.
- ADR 0007's wording ("two turns of the same Conversation") becomes "two AI Cells of the same Notebook".

**Decide**
- **D9. Does an AI Cell's own current Run's tables appear in its later steps' system prompt?** Today they do (`agent.py:624`). #93 says the prompt lists tables "from cells above". Excluding them would (a) hide a table the LLM just made from its own next step, where it now learns of it only from Query Metadata, and (b) break the cross-cell cache, because cell 2's system prompt would then match none of cell 1's requests.
- **D10. What is `request` for a SQL Cell's table, and does the system prompt line change shape for it?** Today it is the turn's user prompt (`agent.py:782`). Candidates are the SQL text, empty, or "SQL Cell" plus the SQL.
- **D11. Should the fixed instructions explain the cell kinds**, e.g. "user messages that start with `SQL Cell:` are queries the user ran themselves; you may state their counts and SHOW rows"? The values rule at `semantic_layer.py:50-55` would otherwise tell the LLM it may not state a SQL Cell's count or a SHOW row that it was legitimately sent.

---

## 5. Table Catalog's `conversation_id` / `turn_id`

**Assumes**
- `result_tables.conversation_id TEXT NOT NULL REFERENCES conversations(id)` and `turn_id INTEGER NOT NULL` (`catalog.py:20-34`; the dataclass is at `catalog.py:74-75`).
- Ownership queries: `owned(conversation_id, turn_id=None)` (`catalog.py:203-207`), `drop_owned` (`catalog.py:362-368`), `tables_of(conversation_id)` (`catalog.py:260-266`), all used by delete and by the system prompt (`agent.py:377-404`, `agent.py:624`).
- `register` writes owner ids **and touches `conversations.last_activity_at`** (`catalog.py:318-331`), so the Catalog reaches into the store's table.
- `sweep` runs only at startup and in `create_conversation` (`agent.py:300-304`; `catalog.py:476-492`).
- There is no notion of *which Run* made a table: a turn runs once.

**Mechanical**
- Rename to `notebook_id` / `cell_id`. The `last_activity_at` touch moves to `notebooks`. `sweep` runs on notebook creation.
- Delete cell / delete notebook map onto `owned` and `drop_owned` with cell and notebook ids (#94 item 1).
- A SQL Cell re-run replaces in place under the same name, with Refresh's swap (#94 item 2), which is the existing `record_refresh` path (`catalog.py:333`). The registry row keeps its owner.

**Decide**
- **D12. How does the Catalog tell a cell's *current* Run tables from its earlier-Run tables** (needed for #94 items 3-4 and for the system prompt)? Option A is a `run_id` column joined to the cell's latest Run. Option B is a flag that is flipped when a new Run starts. A SQL Cell re-run keeps the same table (and name), so its `run_id` would have to be updated in place.
- **D13. At the upgrade that drops Conversations, what happens to existing Result Tables and Saved Tables?** ADR 0004 says Saved Tables are never deleted with their Conversation. The map says Conversations are dropped, not migrated. Options are to keep the registry rows with a dangling or NULL owner (allowed today after a delete, ADR 0003), so they show in the Tables dialog but belong to no cell, or to drop everything. The `NOT NULL REFERENCES conversations(id)` constraint must change either way.

---

## 6. Context meter

**Assumes**
- Context Usage is "how much of the context window *the Conversation* occupies" (`context_usage.py:1`; GLOSSARY).
- The number is the prompt tokens of the **latest real (not Fake Mode) LLM call across all turns** of the Conversation, taken from the stored Traces (`web.py:132-140`; `trace.py:89-94`). The window is that of the **currently picked** model (`web.py:128-130`, `web.py:207-210`).
- `context_usage()` itself is pure arithmetic (`context_usage.py:20-28`) and needs no change.

**Mechanical**
- Per AI Cell, after it runs (#93 item 6): `context_tokens(cell's latest Run Trace)`. There is no notebook-wide meter, so `latest_tokens(conversation_id)` and the `/context-meter?conversation_id=` endpoint go away or become per cell.
- A Fake Mode cell shows "no context used", as today.

**Decide**
- **D14. Which model's window does a run AI Cell's meter use when its settings chip has since been changed (a draft)?** The model it ran with (from its Trace) or the drafted one? Today the meter follows the picker live (`web.py:207-210`).

---

## 7. Flagging

**Assumes**
- `flagging.py` itself is history-agnostic: `find_unseen(text, allowed_texts)` (`flagging.py:76-92`).
- The Agent builds `allowed_texts` from **the messages sent in this step**: the system prompt, every `user` and `tool` message content, and tool-call arguments, but not assistant prose (`agent.py:745-760`). Flags go to the Trace and a FlagEvent (`agent.py:763-769`).

**Mechanical**
- None, if the assembled AI Cell context is what is passed as `messages`. Ticked Markdown Cells count as seen (#93). Unticked ones are absent, so their values are flagged (#93). SQL Cell counts and up to ~100 SHOW rows (#100) count as seen, provided they are in the messages, whatever their role (D1).

**Decide**
- Nothing new beyond D1 and D11. (#93 already settled that ticked Markdown text counts as seen.)

---

## 8. Logging

**Assumes**
- Hardly anything logs Conversation or turn ids. The app logs a failed Refresh (`agent.py:468`) and the dev watcher (`dev.py:43`). `logs.py` is format-only (`logs.py:1-28`). The ids appear only indirectly, in uvicorn access-log URLs (`/c/{conversation_id}`, `/chat/stream/{turn_id}`, `web.py:216-411`).

**Mechanical**
- The URL scheme changes show up in access logs. Nothing else.

**Decide**
- **D15 (minor). Should a failed SQL Cell Run be logged the way a failed Refresh is (`agent.py:466-472`)?** The user already sees the raw error (#97), so the log line is only for the operator.

---

## 9. The store (`store.py`)

**Assumes**
- Four tables: `conversations`, `turns` (`user_message`, `trace`), `messages` (`conversation_id`, `turn_id`, `body`) and `displays` (`conversation_id`, `turn_id`, `after_message_id`, …) (`store.py:27-58`).
- The order of everything is **insertion order** (`ORDER BY id`): `messages()` (`store.py:219-223`) and `transcript()` (`store.py:225-254`), which pins each Trace after its turn's last message.
- `last_activity_at` is touched on every write and orders the sidebar (`store.py:12`, `store.py:90-97`, `store.py:122-128`). Default titles are "Conversation N" (`store.py:99-113`).
- `delete_turn` deletes a turn's messages and Displays (`store.py:142-149`). `delete_conversation` deletes everything (`store.py:135-140`).
- A Count Display Refresh rewrites the Display's number (`store.py:202-209`, `agent.py:474-492`) but **not** the `tool` message the LLM saw.
- The Trace keeps only turn-level `model` and `system_prompt` (`trace.py:3-4`, `trace.py:17-20`). It records neither effort nor row cap, nor the history that was sent.

**Mechanical**
- New shape: `notebooks`; `cells` (notebook, position, kind, draft text, settings, Send to AI, Command); `runs` (cell, started, Trace, context fingerprint #94, SQL text, row cap, status, inline SHOW/DESCRIBE rows #100); `messages` and `displays` keyed by Run. An AI Cell replaces its Run (#94 item 3: no run history), so "latest Run" is the only one kept.
- Order comes from cell position plus within-Run insertion order, not from global `ORDER BY id`.
- Delete cell maps onto `delete_turn`. `delete_preview` is unchanged in shape (`agent.py:377-385`).
- The Trace gains effort and rows (#95: "the Trace line shows what it ran with").
- Existing data is dropped (map). The `CREATE TABLE IF NOT EXISTS` script (`store.py:27-58`) needs an explicit drop or a new file. See D13 for the tables.

**Decide**
- **D16. When a Count Display in a cell is refreshed, does the context of the AI Cells below see the new number, and does it make them stale?** For a SQL Cell count, #94 item 6 says "a count that changed" makes the cells below stale. For an **AI Cell's** `run_count_query` count, the history holds the original `tool` message, and Refresh changes only the Display (`store.py:202-209`). Today the next turn sees the old number.
- **D17. Is a SQL Cell's Query Metadata in the context of the cells below frozen at its Run, or read live from the Catalog?** "Live" follows renames, Refreshes (row count) and expiry, but then a rename or Refresh above changes the fingerprint (stale) and breaks the cache. "Frozen" keeps the cache but gives the LLM a name that no longer exists after a rename. The system prompt already gives current names (#93 item 4).
- **D18. Does a Refresh from a SQL Cell table's name line count as a Run of that cell** (status line "ran …", stored row count, fingerprint for the cells below)? #94 says re-running an unchanged SQL Cell *is* a Refresh. The reverse is not stated.
- **D19. Does the Run record store the exact messages the AI Cell was sent, or only the fingerprint?** #93 item 3 says "its Trace remains the record of what that run was sent", but the Trace today stores only the system prompt and the responses (`trace.py:17-47`), not the history.
- Notebook titles and sidebar ordering (what counts as activity: a draft edit? a move?) belong to the map's "Notebook-level features" item, not here.

---

## 10. Tests

These are rough counts of test functions that create, run, open or delete a Conversation or turn (directly or through `conftest.collect`, which wraps `run_turn` with a `Conversation()`, `tests/conftest.py:377-380`), or that mention the ids. **About 235 of 680 tests in 46 files.** Most of these are mechanical: `Conversation()` becomes a notebook with one AI Cell, and `collect` keeps its signature.

| File | Affected / total | Nature |
|---|---|---|
| test_conversations.py | 24/24 | Conversation CRUD, history, reopen; rewrite as notebook/cell tests |
| test_agent_robustness.py | 21/23 | history repair, failures across turns |
| test_fake_mode.py | 18/29 | turn running; **5 follow-up tests depend on D7/D8** (`:125`, `:189`, `:201`, `:267`, `:281`) |
| test_chaining_expiry.py | 11/13 | multi-turn chaining via the system prompt (D9) |
| test_flagging.py | 10/11 | turn running; allowed context |
| test_delete_tables_choice.py | 10/10 | delete conversation/turn → cell/notebook |
| test_trace.py | 9/23 | per-turn Trace |
| test_describe_table.py | 9/9 | tables from "another Conversation" |
| test_agent_turn.py | 9/9 | the turn loop |
| test_stop_turn.py | 8/8 | stop semantics (D2) |
| test_catalog.py | 7/23 | `owned`, `tables_of`, `register` signatures (D12) |
| test_query_safety.py | 7/16 | turn running only |
| test_charts.py | 7/11 | turn running only |
| test_lookup_tables.py, test_show_tables.py, test_count_display.py | 6 each | turn running; count Refresh (D16) |
| test_meta_conversation_id.py | 4/4 | `_meta` id (D6): delete or rewrite |
| test_show_other_conversations_tables.py | 4/5 | cross-Conversation reads (becomes cross-notebook) |
| test_context_usage.py | 3/10 | meter per Conversation → per cell (D14) |
| test_conversation_switch_browser.py, test_sidebar_browser.py, test_dialogs_browser.py | 3, 1, 4 | page; rewritten with the page |
| ~28 other files | 1-4 each | mostly a single `collect(...)` or `/chat` post |

In addition, `test_eval_runner.py` (23 tests) and `test_smoke_caching.py` (15) drive `run_turn` indirectly through `run_eval` / `smoke_caching.run_turn`. They change only if those entry points change signature. `test_refresh_table.py:212` (system prompt byte-identity) must keep passing under the new table listing.

---

## Summary

### Mechanical (settled by #93–#100; rename or re-plumb)
- `Conversation`/turn becomes Notebook/AI Cell Run across `agent.py`, `store.py`, `catalog.py`, `web.py` routes, `config.py` limit descriptions, the `session_id` "conversation-…" prefix (`agent.py:618-619`) and LLM-facing wording (`semantic_layer.py:112-126`, `mcp_server.py:252-254`).
- The history becomes an AI Cell context assembled from the latest Runs of the cells above, with this Run appended. `repair_history` and flagging reuse it as is.
- The Catalog owner becomes notebook/cell. `sweep` runs on notebook creation. The `last_activity_at` touch moves.
- The row cap and model come from the cell's settings. The Trace records effort and rows.
- Eval: one notebook with one AI Cell per question.
- Context meter: per AI Cell from its Run's Trace.
- ADRs 0002, 0003, 0004, 0006 and 0007 and the GLOSSARY are reworded when built.
- About 235 tests are touched, mostly via `conftest.collect`.

### Needs a decision
1. **D1** What is the wire shape of SQL, SHOW/DESCRIBE and ticked Markdown Cells in the AI Cell context: a `user` message, or a synthetic tool-call/tool-result pair?
2. **D2** Do stopped and failed AI Cell Runs count as "ran" for the cells below?
3. **D3** Does "one cell at a time" include SQL Cells, and how is a running SQL Cell (an uncancellable worker-thread query) stopped?
4. **D4** Should the golden set gain multi-cell cases?
5. **D5** Should the caching smoke check verify caching *across* cells?
6. **D6** Drop `flood_agent/conversation_id` from MCP `_meta` (it is unread), or carry a notebook/cell id?
7. **D7** Should Fake Mode placeholders resolve tables from SQL Cells above?
8. **D8** Do Canned Prompt follow-ups keep the "parent ran above" requirement?
9. **D9** Does an AI Cell's own current-Run tables appear in its later steps' system prompt? (Today yes; excluding them breaks the cross-cell cache.)
10. **D10** What is a SQL Cell table's `request` in the system prompt line?
11. **D11** Should the fixed instructions explain cell kinds, so the LLM may state a SQL Cell count or SHOW rows?
12. **D12** How does the Catalog tell a cell's current-Run tables from its earlier-Run tables (`run_id` or a flag)?
13. **D13** At the upgrade, are existing Result Tables and Saved Tables kept ownerless or dropped?
14. **D14** Does a run AI Cell's meter use the model it ran with, or a drafted model change?
15. **D15** (minor) Is a failed SQL Cell Run logged like a failed Refresh?
16. **D16** Does refreshing a Count Display (especially one from an AI Cell's `run_count_query`) change what the cells below see, and make them stale?
17. **D17** Is a SQL Cell's Query Metadata in the context of the cells below frozen at its Run, or live from the Catalog?
18. **D18** Is a Refresh from a SQL Cell table's name line a Run of that cell?
19. **D19** Does a Run store the exact messages it was sent, or only the fingerprint?
