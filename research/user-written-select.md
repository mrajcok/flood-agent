# What the query path and authorizer already enforce for a user-written SELECT

This is research for the planned **SQL Cell** (issue #96, under map #92). In a SQL Cell the user writes `SELECT <cols> FROM <table> <tail>` in the web app, and no LLM is involved. The FROM table is picked from a drop-down of every table the user can query.

I researched this on 2026-11-13 from the source at commit `db79b5a`. Line numbers refer to that commit. The claims marked **(probed)** were also checked by calling `queries.run_query` / `run_count_query` against throwaway SQLite files. No source code was changed.

## TL;DR

- Every rail lives in `queries.py`: the single-SELECT validator, the read-only Telemetry connection, the authorizer, the timeout, the CTAS with its generated name and row cap, the count-form check and error sanitising. None of it is in `mcp_server.py`. The MCP layer only supplies three inputs: the readable-table set, the timeout and the row cap.
- `queries.run_query` / `queries.run_count_query` take plain arguments. The web app already calls them directly without the LLM, for Refresh (`agent.py:430`, `agent.py:484`), and calls `queries.explain_query` from `/api/explain` (`web.py:451-460`). So a SQL Cell can reuse them without weakening anything.
- The SQL Cell would **bypass** what the Agent does around a tool call:
  - Result Table registration in the Table Catalog. This one is critical. An unregistered `rt_*` table cannot be read, shown or refreshed, and the sweep drops it after an hour.
  - The Count Display row.
  - Trace entries.
  - The per-turn failed-attempt limit.
  - `raw_error` stripping. This one is harmless for a user, since only the LLM must not see raw errors.
- Registration and Displays both need a `conversation_id` **and a `turn_id`** (NOT NULL in `result_tables` and `displays`), and a Display also needs an `after_message_id`. A SQL Cell therefore has to create or attach to a turn and message, or the schema has to change.
- No code decides whether a query is a count query. The **LLM decides** by choosing which tool to call. `run_count_query` only *checks* the form with `_COUNT_FORM_RE`. A SQL Cell needs its own rule. The natural one is to reuse that regex.
- Gaps that also exist today on the LLM path:
  - The authorizer does not restrict Telemetry Database reads at all.
  - `results.sqlite_master` is readable.
  - The validator rejects unquoted identifiers that are SQL keywords, such as `analyze`.
  - The row cap has no upper bound.
- **Ocient:** there is no Ocient code at all. Every enforcement mechanism in the code is SQLite-specific: the `mode=ro` open, ATTACH, `set_authorizer`, the progress-handler timeout, `sqlite_master`/`PRAGMA` and the UDFs. The claim in `todo.md` that "RBAC will prevent them from reading any table they don't have access to" is an assumption that nothing in the repo implements or tests.

---

## 1. What is enforced today for LLM-written SQL

### 1.1 Single statement, SELECT-only (the validator)

`queries.validate_select` (`queries.py:126-142`) runs first in `run_query` (`queries.py:281-284`), `run_count_query` (`queries.py:363-366`) and `explain_query` (`queries.py:325`).

- It tokenises past `--` and `/* */` comments, `'…'`, `"…"` and `` `…` `` quotes (including doubled-quote escapes) and `[…]` identifiers (`queries.py:83-123`).
- The first word must be `select` or `with` (`queries.py:130-131`).
- No unquoted word may be in `_FORBIDDEN_WORDS`, which covers insert, update, delete, drop, create, alter, attach, detach, pragma, vacuum, reindex, analyze, begin, commit, rollback, savepoint and release (`queries.py:71-75`, `132-133`).
- At most one `;` is allowed, and it must come last, with only comments or whitespace after it. It is stripped (`queries.py:134-141`).
- On any rejection it raises `QueryRejected("Only a single read-only SELECT statement is allowed.")` (`queries.py:76-80`).

Caveats that matter more once a human types the SQL:

- The word check runs on bare words, so an **unquoted identifier that is a forbidden keyword is rejected**. For example, `SELECT a AS analyze FROM flows` is rejected **(probed)**. A quoted `"analyze"` passes. Words that merely contain a keyword, such as `update_n`, pass **(probed)**.
- The validator is a heuristic, not a parser. ADR 0001 places the real guarantee in the read-only open and the separate results database, "even if validation is bypassed" (`docs/adr/0001-no-telemetry-data-through-the-llm.md:9`). The authorizer is the third layer.

### 1.2 Connection and authorizer (what can be read and written)

`queries.open_connection` (`queries.py:213-223`):

- opens the Telemetry Database as `file:…?mode=ro` (`queries.py:218`);
- ATTACHes the results database as `results`, read-write (`queries.py:219`). The ATTACH happens before the authorizer is installed, so it is allowed;
- registers the UDFs `in_cidr` and `now` (`queries.py:220-221`);
- installs `_authorizer(allowed_tables)` (`queries.py:222`).

`_authorizer` (`queries.py:192-210`) allows:

- `SQLITE_SELECT`, `SQLITE_READ`, `SQLITE_FUNCTION` and `SQLITE_RECURSIVE` (`queries.py:183-188`, `200`);
- `CREATE_TABLE`, `INSERT` and `UPDATE` **only when `dbname == "results"`**, for the CTAS (`queries.py:189`, `206-207`).

Everything else is denied (`queries.py:208`). That includes ATTACH, PRAGMA, DDL/DML elsewhere and temp tables. A READ in the `results` database is denied unless the table is in `allowed_tables` (lower-cased) or its name starts with `sqlite_` (`queries.py:201-204`).

Which tables are readable:

- **Result, Saved and Lookup Tables.** The MCP server passes `catalog.readable()` (`mcp_server.py:228`, `244`). That is every live Result and Saved Table from any Conversation, plus every registered Lookup Table (`catalog.py:209-212`; ADR 0003, `docs/adr/0003-…:3`, `:7`). "Live" means not dropped and not past its Retention, worked out on every read (`catalog.py:225-227`). Lookup status comes from the registry, not the `lookup_` prefix (`catalog.py:162-171`).
- **An unregistered or expired table in the results DB is denied.** The probe returned `access to results.rt_other.y is prohibited`, sanitised to `Access to a table or column was denied.` **(probed)**.
- **The Telemetry Database is not restricted at all.** The authorizer checks names only for `dbname == "results"`, so *any* table in the Telemetry file is readable, including tables not in the Semantic Layer **(probed: a non-Semantic-Layer table `secret` was read)**. The "telemetry" kind in the Catalog is only a name list from the Semantic Layer (`catalog.py:137`, `173-176`; `agent.py:288`, `358`), and it is not an access control.
- **`results.sqlite_master` is readable** because of the `sqlite_` exemption (`queries.py:203`). That exposes the names and DDL of every table in the results file, including expired ones and other Conversations' tables **(probed)**. This is consistent with ADR 0003's single-user boundary, but it is wider than `readable()`.
- When an unqualified name exists in both databases, SQLite resolves it to `main` first. Renames to a Telemetry table name are refused (`catalog.py:416-417`), so a Saved Table cannot shadow a Telemetry table.

The authorizer stays installed during the CTAS. It is removed only for the follow-up metadata reads (`queries.py:301`).

### 1.3 CTAS, Result Table naming and the row cap

`queries.run_query` (`queries.py:263-315`):

- **Name:** `_free_name` returns `rt_<YYYYMMDD>_<HHMMSS>`, with `_2`, `_3`… on collision, checked against `results.sqlite_master` (`queries.py:55-68`). It retries up to 5 times if a concurrent query takes the name (`queries.py:290-298`). The name pattern is `table_names.GENERATED_RE` (`table_names.py:16-17`). The `rt_` prefix is what makes a table a Result Table rather than a Saved Table (`table_names.py:28-30`; `catalog.py:217`). `rt_` and `lookup_` are reserved for user renames (`table_names.py:14`, `24-25`; `catalog.py:414-415`).
- **Executed SQL:** `CREATE TABLE results.<name> AS SELECT * FROM (\n<select>\n) LIMIT <max_rows>` (`queries.py:291`). The newlines stop a trailing `--` comment in the user SQL from swallowing the wrapper. The parentheses are *not* checked for balance: `SELECT * FROM flows) UNION SELECT x FROM (secret` runs **(probed)**. It is still a SELECT under the authorizer, so it gains nothing the authorizer forbids.
- **Row cap:** `LIMIT int(max_rows)`. When `row_count >= max_rows`, the result gets `truncated_to` and an LLM-facing `note` (`queries.py:230-233`, `311-314`).
  - On the MCP path the cap comes **only** from the request `_meta` key `flood_agent/max_result_rows`. It must be an int ≥ 1, with no default (`mcp_server.py:90-96`; `mcp_protocol.py:7`; set by the Agent in `mcp_connection.py:111`).
  - The web app parses the user's "Max result rows" choice with `_parse_max_rows`: a whole number ≥ 1, with **no upper bound** (`web.py:523-528`; the UI default is 10000, `templates/index.html:540`).
- **The `title` argument is accepted but unused** by `queries.run_query` (`queries.py:270`). Titles are recorded by the Agent at registration instead (`agent.py:780-783`).

### 1.4 Timeout

`_deadline` installs a SQLite progress handler that aborts the statement after `timeout_s` seconds, checked every 1000 VM steps (`queries.py:236-254`). When the deadline fires, the error is replaced with `_TIMEOUT_GUIDANCE`, which tells the user to aggregate, narrow `etime` or filter (`queries.py:226-229`, `257-260`). The timeout comes from config `query_timeout_s` (> 0, `config.py:71`; default 30 s in `defaults.toml:15`), which reaches the MCP server through `QUERY_TIMEOUT_S` (`mcp_server.py:52`, `68`). The same deadline wraps `run_count_query` (`queries.py:371-375`) and `explain_query` (`queries.py:328-334`).

### 1.5 Count queries: how they are recognised and run without CTAS

- **The LLM decides that a query is a count query, by calling the `run_count_query` tool** instead of `run_query` (`mcp_server.py:238-247`). No code looks at a query and routes it.
- `queries.run_count_query` (`queries.py:353-383`) then *checks* the query:
  1. It runs `validate_select`.
  2. The SQL must match `_COUNT_FORM_RE`, `^\s*select\s+count\s*\([^()]*\)\s*(?:as\s+\w+\s+)?from\s` (`queries.py:349-350`). That means one bare `COUNT(...)` with no nested parentheses and an optional alias, followed by FROM.
  3. It runs `SELECT * FROM (\n<select>\n) LIMIT 2` with no CTAS (`queries.py:373`).
  4. The result must be one row with one non-negative, non-bool int (`queries.py:378-382`).
  5. It returns `{"success": true, "count": n}`.

  If any check fails, the reply is `_COUNT_GUIDANCE` (`queries.py:345-348`). Examples: `SELECT count(*)+0 …` is rejected and `SELECT count(*) AS n …` is accepted **(probed)**.
- The count form is the ADR 0001 exception: the one telemetry-derived value that reaches the LLM (`docs/adr/0001-…:10`; `GLOSSARY.md:75-77`).
- The Count Display is made by the Agent and not by the server. `Agent._count_display` turns a successful `run_count_query` into a `CountEvent` with the title, value, SQL and time (`agent.py:859-871`). It is persisted as a `count` Display with its turn (`agent.py:673-677`; `store.py:25`, `171-193`). Refresh runs `run_count_query` again from the web app (`agent.py:474-492`; `web.py:462-470`).

### 1.6 Query Metadata and errors

- **Success:** `{success, result_table, columns:[{name,type}], row_count, executed_sql}`, plus `truncated_to`/`note` when the result was truncated (`queries.py:310-315`). Column types are the declared type, or, for computed columns, the `typeof` of the first non-NULL value, else `ANY` (`queries.py:397-408`). The Agent removes `executed_sql` before the LLM sees the metadata and keeps it for the Trace (`agent.py:738-740`, `786-796`).
- **Failure:** `_failure` returns `{success:false, error:<sanitised>, raw_error:<SQLite text>}` (`queries.py:257-260`).
  - `sanitize_error` replaces any authorizer denial with `ACCESS_DENIED` and redacts `results.`/`main.`/`temp.`-qualified names, quoted literals and numbers (`queries.py:163-180`).
  - The Agent removes `raw_error`, emits a `query_error` Trace entry and counts the failure (`agent.py:807-822`).
  - A validation rejection has no `raw_error` (`queries.py:283-284`).
- **Retry limit:** failed `run_query`/`run_count_query` calls count toward `max_failed_attempts` per turn (`mcp_protocol.py:9-10`; `config.py:73-75`; `agent.py:703-706`).

### 1.7 What the MCP/Agent layer adds around `queries.*`

The MCP server adds:

- the `readable()` set (`mcp_server.py:228`);
- the row cap from `_meta` (`mcp_server.py:234`);
- the configured timeout;
- running the call in a worker thread (`mcp_server.py:79-87`).

The Agent, in `_run_tool`, adds:

- JSON and argument checks (`agent.py:711-722`);
- **registration of the Result Table in the Catalog** straight after the call: `Catalog.register(name, conversation_id, turn_id, title, columns, request, truncated_to, sql, max_rows)` (`agent.py:729-734`, `771-785`; `catalog.py:317-331`);
- the Trace entries (`agent.py:669`, `799-822`);
- the Count Display (`agent.py:673-677`).

---

## 2. What would be missing or bypassed if the SQL came from the web app

| Rail | Where it lives | SQL Cell calling `queries.*` directly |
|---|---|---|
| Single SELECT, forbidden words, one trailing `;` | `queries.validate_select` | **Kept.** |
| Read-only Telemetry open; results DB separate | `queries.open_connection` | **Kept.** |
| Authorizer (action allow-list; results reads limited to `allowed_tables`) | `queries._authorizer` | **Kept if** the caller passes `agent.tables.readable()`, as Refresh (`agent.py:432`, `486`) and `/api/explain` (`web.py:457`) do. Passing `None` would mean *no* results-DB table is readable (`queries.py:197`). |
| Timeout | `queries._deadline` | **Kept** if `agent.config.query_timeout_s` is passed. |
| CTAS under a generated `rt_*` name, `LIMIT max_rows` | `queries.run_query` | **Kept.** The cap must come from the user's "Max result rows" choice (`web.py:523-528`), which has no upper bound today. |
| Count form check, single non-negative int | `queries.run_count_query` | **Kept**, but **which function to call** is no longer chosen by an LLM (see §1.5). The SQL Cell must decide, for example by matching `queries._COUNT_FORM_RE` (private today, `queries.py:350`). |
| **Catalog registration** (Conversation, turn, title, sql, max_rows, truncation) | `Agent._register_result_table` (`agent.py:771-785`) | **Bypassed.** Without it the table is not live, so it is not in `readable()`. The authorizer then denies reads of it, `show_table`/`show_chart` and the `/api/results/...` routes 404, and Refresh does not work (`catalog.py:229-238`, `209-212`). `Catalog.sweep` drops an unregistered `rt_*` table after an hour (`catalog.py:476-493`). |
| Registration needs a Conversation **and turn** | `result_tables.conversation_id`/`turn_id` NOT NULL (`catalog.py:21-25`) | **Missing:** a SQL Cell has no turn. One has to be created or the schema changed. |
| Display persistence (table Display / Count Display) | `Store.add_step` (`store.py:168-193`); `displays.turn_id` NOT NULL and `after_message_id` NOT NULL (`store.py:48-56`) | **Missing:** Displays hang off a turn and a message, and messages are the LLM history. Deciding where a SQL Cell's output lives, and whether the LLM sees it, belongs to another ticket. |
| `raw_error` stripping, `executed_sql` stripping, error sanitising | `Agent._split_query_error`, `_strip_executed_sql`; `queries.sanitize_error` | **Not needed for the user.** The precedent shows the user the plain error: `/api/explain` returns `str(e)` (`web.py:458-459`) and Refresh shows the sanitised `error` and logs `raw_error` (`agent.py:464-472`). The user may see `raw_error`, which is more useful for hand-written SQL. Sanitising matters only if the error is later sent to the LLM. |
| Trace entries (generated SQL, query_error) | `TraceBuilder` via `_run_tool` | **Bypassed.** There is no Trace for a SQL Cell. |
| Per-turn failed-attempt limit | `max_failed_attempts` (`config.py:73-75`) | **N/A:** the user retries by hand. |
| Truncation `note` | `queries._TRUNCATED_NOTE` (`queries.py:230-233`) | The wording is aimed at the LLM ("Do not present it as the complete answer"). The UI needs its own message. `truncated_to` is enough. |
| `FIXED_NOW_MS` pinning of `now()` | env var read by `queries.now_ms` (`queries.py:145-149`); passed to the MCP subprocess only (`mcp_server.py:58-59`) | In the web process `now()` uses the web process's environment, as Refresh already does. This matters only for evals. |
| Off the event loop | `_in_worker_thread` (`mcp_server.py:79-87`) | FastAPI runs a sync `def` route in its thread pool. The existing `/api/explain` and refresh routes are sync `def` (`web.py:439`, `452`, `463`). |

Other considerations for user input:

- **The FROM drop-down is not a boundary.** The cell assembles `SELECT <cols> FROM <table> <tail>`. Both `<cols>` and `<tail>` can contain subqueries and joins to other tables, so the authorizer, not the drop-down, decides what is read. The whole assembled string has to go through `validate_select`. A `;` in the tail is rejected unless it is trailing (`queries.py:134-141`).
- **The drop-down source already exists.** `CatalogView.listing()` returns the telemetry, lookup, saved and result names (`catalog.py:190-201`), served at `GET /api/tables` (`web.py:326-329`).
- **There is no authentication.** "Flood Agent is single-user and local, with no authentication" (`README.md:83`), and ADR 0003 makes the deployment's single user the boundary (`docs/adr/0003-…:3`). `/api/explain` already takes arbitrary SQL over HTTP (`web.py:451-460`). A SQL Cell endpoint adds CTAS writes and row reads to that surface.

### Natural entry point

- **For a table result:** `queries.run_query(agent.config.telemetry_db, agent.config.results_db, sql, agent.config.query_timeout_s, max_rows, agent.tables.readable())` (`queries.py:263-315`), followed by `agent.tables.register(...)` (`catalog.py:317-331`). The closest existing template is `Agent.refresh_table` (`agent.py:418-450`): the same call with no LLM, with errors turned into a plain message by `_refresh_error` (`agent.py:464-472`). A new `Agent` method next to it, for example `run_user_query(conversation_id, turn_id, sql, max_rows)`, would bundle run, register and Display. That keeps `web.py` thin, as the refresh routes are (`web.py:438-449`).
- **For a count:** `queries.run_count_query(...)` (`queries.py:353-383`) plus a Count Display row, modelled on `Agent.refresh_count` (`agent.py:474-492`) and `_count_display` (`agent.py:859-871`).
- **For "is this a count query?":** `queries._COUNT_FORM_RE` (`queries.py:350`), made public or wrapped in a helper.

---

## 3. Ocient (production) versus SQLite

There is **no Ocient code in the repository**. Every query path uses `sqlite3`. What the repo says about production:

- **RBAC is assumed, not implemented.** `todo.md:2` says a user SELECT "is safe since RBAC will prevent them from reading any table they don't have access to". ADR 0003 says the boundary "is now the deployment's single user (in production, that user's Ocient schema)" (`docs/adr/0003-…:3`). Nothing in code, tests or ADRs says which Ocient account the app connects as, or how a per-user role would be applied.
- **Every enforcement mechanism is SQLite-specific** and would need an Ocient equivalent:
  - the read-only open `?mode=ro` and the `ATTACH … AS results` split (`queries.py:218-219`), which is ADR 0001's "even if validation is bypassed" guarantee (`docs/adr/0001-…:9`). On Ocient this becomes a matter of grants;
  - `set_authorizer` with its action allow-list and the results-table allow-list (`queries.py:192-210`, `222`). Ocient has no per-connection authorizer callback, so the `readable()` restriction (live, registered tables only) would have to come from grants or from rewriting or parsing the SQL. **RBAC on its own would not enforce Retention or liveness:** an expired but undropped table would stay readable until `sweep` drops it (`catalog.py:476-493`);
  - the progress-handler timeout (`queries.py:236-254`). Ocient needs a server-side or driver timeout;
  - `_free_name` reads `results.sqlite_master` (`queries.py:62`). `run_query`'s metadata uses `PRAGMA results.table_info` and `typeof` (`queries.py:304`, `397-408`);
  - the forbidden-word list is SQLite's statement keywords (`queries.py:71-75`). Because the first word must be `select`/`with`, Ocient-only statements are still rejected.
- **SQL dialect differences.** Ocient's `now()` returns a timestamp and needs converting to epoch ms (`semantic_layer.yaml:33`). `in_cidr` is a SQLite UDF that "needs an Ocient mapping" (`queries.py:152-154`; `semantic_layer.yaml:39`). Range filters belong on the Time Key `etime`, which is the partition key in Ocient (`GLOSSARY.md:29-30`; `semantic_layer.yaml:3`). A user writing SQL by hand has no Semantic Layer prompt to remind them of this.
- **CTAS.** "Ocient supports CTAS, so the pattern carries over to production" (`docs/adr/0001-…:8`). However, Ocient CTAS does not inherit TimeKey or clustering keys, and whether a key-less CTAS works has **not been tested** (`docs/research/ocient-table-replacement.md:10`, `:34`).
- **Names.** Generated and user names are kept to plain identifiers that are valid unquoted in both SQLite and Ocient (`table_names.py:1-7`). Names are lower-cased (ADR 0005, `docs/adr/0005-…:3`).
- **Refresh swap** is one transaction on SQLite and a drop-then-rename (or `CREATE OR REPLACE`) on Ocient, which is unverified (`docs/adr/0006-…:12`). This matters only if a SQL Cell's table is later refreshed.

## Sources

Everything was read from the repository at `db79b5a`: `src/flood_agent/{queries,catalog,mcp_server,mcp_protocol,mcp_connection,agent,web,store,lookup_tables,table_names,config}.py`, `src/flood_agent/defaults.toml`, `src/flood_agent/templates/index.html`, `semantic_layer.yaml`, `todo.md`, `README.md`, `GLOSSARY.md`, `docs/adr/0001`, `0003`, `0005` and `0006`, and `docs/research/ocient-table-replacement.md`. The probes called the real `queries.run_query`/`run_count_query` against temporary SQLite files.
