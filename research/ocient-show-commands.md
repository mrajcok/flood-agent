# Ocient SHOW commands: what exists and what they return

Research for issue #99 (child of map #92), feeding decision #100 (allow `SHOW` in SQL Cells). Researched 2026-11-13 from the Ocient docs (read from the docs' full-text export, `https://docs.ocient.com/llms-full.txt`, so quotes are the page text) and from the source of the `pyocient` 3.10.1 driver wheel on PyPI. **Nothing here has been run against a real Ocient system.**

## 1. Summary

- Ocient has **16 documented `SHOW` statements**, all listed on one page, [Information Schema § SHOW Commands](https://docs.ocient.com/information-schema). Each is a shortcut for a view in `information_schema`: "These commands are tightly coupled with the `information_schema` schema and offer a more user-friendly syntax alternative. Sometimes, unlike their `information_schema` counterparts, `SHOW` commands only return user-defined objects."
- **The syntax is only the bare keywords** (`SHOW TABLES;`). No doc shows a `LIKE`, `FROM <table>`, `IN <schema>` or `WHERE` clause on any of them. So `SHOW COLUMNS` returns columns of *every* table you can access. There is no documented way to restrict it to one table.
- **No doc says whether a `SHOW` can be a subquery, a CTE body or the query of a CTAS.** CTAS's `query` is documented as "A `SELECT` query" ([Tables](https://docs.ocient.com/tables)). Assume `SHOW` is a top-level statement that only returns rows to the client. The equivalent `SELECT … FROM information_schema.<view>` is a plain SELECT. The docs show it with `WHERE` and `LIKE` ([Quick Start Querying](https://docs.ocient.com/quick-start-querying)), so it is the composable form.
- **No `SHOW` returns table data.** All of them return metadata. Closest to "values": `SHOW VIEWS` returns `view_definition` (the view's SQL, which can contain literals), `SHOW COLUMNS` probably returns `column_default` (if it mirrors the view), `SHOW PIPELINE_STATUS` returns load counters, and `SHOW TABLE_PRIVILEGES`/`SHOW USERS`/`SHOW GROUPS` return security information. Real data statistics, such as min/max values, live in `sys.*` catalog tables reached by ordinary `SELECT` (see §5), not in `SHOW`.
- **RBAC.** Every object-scoped `SHOW` is documented as filtered to what "the current user has access" to, or has "read access" to. Three are not filtered: `SHOW DATA_TYPES`, `SHOW RESERVED_WORDS` and `SHOW SYSTEM TABLES`. The docs do not say whether the matching `information_schema` views are filtered the same way (§4).
- **Driver.** `pyocient` sends `SHOW …` down the same `ExecuteQuery` path as `SELECT`/`WITH` and fetches a result set. One exception: the first words `SHOW STATS`/`SHOW STATISTICS` are sent as an *update*. That form is undocumented (§3).

## 2. The documented SHOW statements

The descriptions are quoted from [Information Schema § SHOW Commands](https://docs.ocient.com/information-schema). The "likely columns" come from the matching `information_schema` view on the same page, **except** where a documented example of the `SHOW` output exists (marked *example*). The docs never give the `SHOW` output columns as a table. The examples show that the `SHOW` output does not always match the view (§2.1).

| Statement | Doc description (RBAC wording in bold) | Backing view | Likely columns | Rows |
|---|---|---|---|---|
| `SHOW COLUMNS` | "returns columns from user-defined tables and views **where the current user has access**" | `information_schema.columns` | table_catalog, table_schema, table_name, column_name, ordinal_position, data_type, is_nullable, column_default | one per column of every accessible user table/view: can be large |
| `SHOW DATA_TYPES` | "returns all data types in the system" | `information_schema.data_types` | data_type | fixed list, tens |
| `SHOW DATABASES` | "returns all databases **where the current user has access**" | `information_schema.databases` | database_name, created_at (*example* shows `database_name`, `created`) | few |
| `SHOW GEOMETRY_COLUMNS` | "returns all geometry type columns **where the current user has access**" | `information_schema.geometry_columns` | f_table_catalog, f_table_schema, f_table_name, f_geometry_column, coord_dimension, srid, type | usually 0 or few |
| `SHOW GROUPS` | "returns all groups in the system **where the current user has read access**" | `information_schema.groups` | database_name, group_name | few |
| `SHOW INDEXES` | "returns all indexes on user-defined tables **where the current user has access**" | `information_schema.indexes` | table_catalog, table_schema, table_name, index_name, index_type, column_name | one per index column |
| `SHOW NODES` | "returns all nodes in the system **where the current user has read access**" | `information_schema.nodes` | name, service_role_types (VARCHAR[]), operational_status, software_version | one per cluster node |
| `SHOW PIPELINE_STATUS` | "returns the status of all pipelines in the system **where the current user has read access**" | `information_schema.pipeline_status` | *example*: database_name, pipeline_name, table_names, status, status_message, percent_complete, duration_seconds, files_processed, files_failed, files_remaining, files_total, records_processed, records_loaded, records_failed | one per pipeline |
| `SHOW PIPELINES` | "returns all pipelines in the system **where the current user has read access**" | `information_schema.pipelines` | *example*: database_name, pipeline_name, loading_mode, source_type, data_format, created_at, created_by, table_names | one per pipeline |
| `SHOW RESERVED_WORDS` | "returns all reserved words in the system" | `information_schema.reserved_words` | reserved_word | fixed list, hundreds |
| `SHOW SCHEMATA` | "returns all user-defined schemas **where the current user has access**" | `information_schema.schemata` | schema_name, catalog_name | few |
| `SHOW SYSTEM TABLES` | "returns all system tables in the `sys` or `information_schema` schemas" | `sys.system_tables` / `information_schema.tables` (`table_type` `SYSTEM TABLE`/`SYSTEM VIEW`) | not documented. `sys.system_tables` gained `schema`, `type`, `description` when this statement was added ([release notes](https://docs.ocient.com/ocientaiq-unified-data-platform-release-notes)) | ~hundreds (fixed per version) |
| `SHOW TABLE_PRIVILEGES` | "returns all table privileges for tables **where the current user has access**" | `information_schema.table_privileges` | grantor, grantee, table_catalog, table_schema, table_name, privilege_type, is_grantable | one per grant |
| `SHOW TABLES` | "returns all user-defined tables **where the current user has access**" | `information_schema.tables` | *example*: table_catalog, table_schema, table_name, table_type, is_insertable_into, created_at | one per accessible user table |
| `SHOW USERS` | "returns all users in the system **where the current user has read access**" | `information_schema.users` | database_name, user_name | one per user |
| `SHOW VIEWS` | "returns all user-defined views **where the current user has access**" | `information_schema.views` | *example*: table_catalog, table_schema, table_name, view_definition, created_at, updated_at, creator_id | one per accessible view |

Sources for the examples: `SHOW TABLES` and `SHOW DATABASES`: [Other Functions and Expressions](https://docs.ocient.com/other-functions-and-expressions). `SHOW PIPELINES` and `SHOW PIPELINE_STATUS`: [Monitor Data Pipelines](https://docs.ocient.com/monitor-data-pipelines). `SHOW VIEWS`: [Object-Type Level Privileges Management](https://docs.ocient.com/object-type-level-privileges-management). `SHOW SYSTEM TABLES`: [release notes](https://docs.ocient.com/ocientaiq-unified-data-platform-release-notes) ("The `SHOW SYSTEM TABLES` SQL statement displays the system catalog tables").

### 2.1 The SHOW output and its view can differ

- The `SHOW PIPELINES` example has `created_by` but no `status`, `altered_at`, `creator_id` or `transaction_id`, though all of these are in `information_schema.pipelines`.
- The `SHOW PIPELINE_STATUS` example has `percent_complete`. The view has `fraction_complete` and `bytes_ingested`.
- The `SHOW DATABASES` example has `created`. The view has `created_at`.

So the docs only pin down the columns of each `SHOW` statement loosely. The examples may come from older versions. Code that relies on exact column names should use the `information_schema` view, whose columns are documented, or should read the result's own column description.

## 3. Other read-only metadata statements (not `SHOW`, but SHOW-like)

| Statement | Returns | Privilege | Source |
|---|---|---|---|
| `EXPORT TABLE t` | one text value: the full `CREATE TABLE …` DDL (columns, TimeKey, clustering/indexes, redundancy, storage space) | `SELECT TABLE` on the table | [Tables § EXPORT TABLE](https://docs.ocient.com/tables) |
| `EXPORT VIEW v` | the `CREATE VIEW` statement | system-level user or `READ VIEW` | [Views § EXPORT VIEW](https://docs.ocient.com/views) |
| `EXPORT PIPELINE p` / `EXPORT PIPELINE FUNCTION f` | the `CREATE PIPELINE` / `CREATE PIPELINE FUNCTION` statement. A pipeline's SQL can include source credentials or paths (not checked). | not stated | [Data Pipelines](https://docs.ocient.com/data-pipelines) |
| `EXPLAIN [PROTO\|JSON\|DEBUG] query` / `EXPLAIN PIPELINE …` | the optimized plan, without running the query | not stated | [Query Analysis](https://docs.ocient.com/query-analysis) |
| `DESCRIBE TABLE [schema.]t [verbose]`, `DESCRIBE VIEW [schema.]v [verbose]` | a table's columns and types, or a view's query text | not stated | documented only as JDBC **CLI** commands ([JDBC CLI commands](https://docs.ocient.com/commands-supported-by-the-ocient-jdbc-cli-program)). The pyocient 3.5.0 notes say "Added server-side `DESCRIBE TABLE` and `DESCRIBE VIEW` command support with fallback to the client-side `DESCRIBE` command for older server versions" ([pyocient release notes](https://docs.ocient.com/pyocient-release-notes)), so newer servers accept them as SQL. This server-side form has no SQL reference page. |
| `LIST TABLES`, `LIST VIEWS`, `LIST INDEXES`, `LIST SYSTEM TABLES`, `LIST ALL QUERIES`, `GET SCHEMA` | CLI-only commands, implemented in the client, not SQL | — | [JDBC CLI commands](https://docs.ocient.com/commands-supported-by-the-ocient-jdbc-cli-program) |
| `PREVIEW PIPELINE` | **source data rows**, transformed, without loading them | not stated | [Data Pipelines](https://docs.ocient.com/data-pipelines). This is the one read-only statement that returns real data values, from outside the database. |

**How pyocient routes these** (from the `pyocient` 3.10.1 wheel, `pyocient/api.py`, `Cursor.execute`): the first word picks the request type. `select`, `with`, `show`, `describe`, `preview`, `check` and `export` go to `ExecuteQuery`, and the cursor reads result metadata for all of them except `export`, which is given a single synthetic `export` CHAR column. `explain` uses a separate `ExecuteExplain` request. There is one undocumented special case: when the second word is `stats` or `statistics` after `show`, `check` or `export`, the statement goes to `_execute_update` and no rows come back. No docs page mentions `SHOW STATS`, so it is unconfirmed what it does. A SQL Cell allow-list should match on whole known keywords, not on a `SHOW` prefix. The pyocient CLI also handles `show scope_id` and `show scope_prefix_len` locally (`pyocient/cli.py`). These are client-side transaction-scope helpers, not server SQL.

## 4. RBAC

- The `SHOW` descriptions above have the filtering words in bold. Object-scoped commands (tables, views, columns, indexes, schemas, databases, geometry columns, table privileges) return only objects "where the current user has access". System-scoped ones (users, groups, nodes, pipelines, pipeline status) return only those "where the current user has read access". `SHOW DATA_TYPES`, `SHOW RESERVED_WORDS` and `SHOW SYSTEM TABLES` have no filter wording, which makes sense for global lists.
- The worked example in [Object-Type Level Privileges Management § Verify Privileges](https://docs.ocient.com/object-type-level-privileges-management) shows the filtering. A user whose group has `VIEW` on views (plus `USE`/`SELECT` on the database) runs `SHOW VIEWS` and sees only the view they may see: "The user has access to the `test` view only."
- Privileges that govern visibility ([DCL reference](https://docs.ocient.com/data-control-language-dcl-statement-reference)): `VIEW [SCHEMA, TABLE, VIEW]`, "the privilege to see and read information of the specified object type within the system". Query history in `sys.queries`/`sys.completed_queries` is gated separately by `VIEW QUERIES` / `VIEW REDACTED QUERIES`, and what you see also depends on whether you log in to `SYSTEM` or to a user database.
- **Not confirmed:** whether `SELECT * FROM information_schema.tables` (and the other views) is filtered the same way as `SHOW TABLES`. The view descriptions say "all tables … in the system" (only `databases` says "where the current user has access"). Read literally, that suggests the views may be less filtered than the `SHOW` commands, or that the descriptions are just loose. Test this before treating `information_schema` as a safe substitute.

## 5. Data values versus metadata

- No `SHOW` returns rows from user tables.
- Content that is metadata in form but can carry data: `view_definition` (`SHOW VIEWS`) and `column_default` (columns) can contain literals from the SQL. `SHOW PIPELINE_STATUS` has `status_message` and record and file counts. `SHOW TABLE_PRIVILEGES`, `SHOW USERS` and `SHOW GROUPS` expose who has access to what.
- **Statistics with data values are in `sys` tables, not in `SHOW`.** For example, `sys.column_distributions` has `minimum_value`, `maximum_value` and `num_samples` per column, and `sys.column_cardinalities` holds cardinalities ([System Catalog](https://docs.ocient.com/system-catalog)). Any user who can `SELECT` from `sys.*` can already read these through an ordinary SQL Cell `SELECT`. Allowing `SHOW` does not change that. The docs do not say how `sys.*` tables are permission-filtered, apart from the query tables in §4.

## 6. Subqueries and CTAS

- Not documented either way for `SHOW`. CTAS takes `AS (query)` where `query` is "A `SELECT` query" ([Tables](https://docs.ocient.com/tables)). The DQL grammar does not list `SHOW` as a table expression. **Assume a `SHOW` can only be run as a top-level statement.** So a SQL Cell cannot wrap it in `CREATE TABLE … AS` the way it does SELECT/WITH. To get a Result Table, either (a) run the `SHOW`, fetch the rows, and insert them into a new table app-side, or (b) rewrite to the backing `SELECT … FROM information_schema.<view>`, which can be filtered and wrapped. Option (b) has the RBAC caveat in §4 and the column differences in §2.1.
- **Not confirmed:** whether Ocient allows CTAS from `information_schema`/`sys` views at all. Information-schema views are virtual and served by the SQL node. CTAS writes segments through the loader path (CTAS also requires explicit TimeKey and clustering setup, see `docs/research/ocient-table-replacement.md`). Test this before relying on it.

## 7. SQLite equivalents (development database)

SQLite has no users or privileges, so there is nothing to filter. All of these are verified on SQLite 3.46 locally. A `PRAGMA x(...)` statement cannot be a subquery or CTAS (`CREATE TABLE r AS PRAGMA …` gives a syntax error). Its table-valued form `pragma_x(...)` is an ordinary SELECT source and *can* be wrapped: "PRAGMAs that return results and that have no side-effects can be accessed from ordinary SELECT statements as table-valued functions", available since 3.16.0 ([PRAGMA docs](https://www.sqlite.org/pragma.html#pragfunc)).

| Ocient | Nearest SQLite | Notes |
|---|---|---|
| `SHOW TABLES` | `SELECT name FROM sqlite_schema WHERE type='table' AND name NOT LIKE 'sqlite_%'`, or `SELECT * FROM pragma_table_list` (3.37+: schema, name, type, ncol, wr, strict) | [Schema table](https://www.sqlite.org/schematab.html). `sqlite_master` is the older alias. |
| `SHOW VIEWS` | `SELECT name, sql FROM sqlite_schema WHERE type='view'` | `sql` matches `view_definition` |
| `SHOW COLUMNS` | `SELECT m.name AS table_name, p.* FROM sqlite_schema m, pragma_table_info(m.name) p WHERE m.type IN ('table','view')` | `pragma_table_info` returns cid, name, type, notnull, dflt_value, pk. To cover all tables you must join it as shown. |
| `SHOW INDEXES` | `SELECT * FROM sqlite_schema WHERE type='index'`, or `pragma_index_list(t)` (seq, name, unique, origin, partial) plus `pragma_index_info(idx)` | |
| `SHOW SCHEMATA` / `SHOW DATABASES` | `SELECT * FROM pragma_database_list` (seq, name, file) | `main`, `temp` and attached databases |
| `SHOW SYSTEM TABLES` | `SELECT name FROM sqlite_schema WHERE name LIKE 'sqlite_%'` | only `sqlite_schema`, `sqlite_sequence`, `sqlite_stat1`… |
| `SHOW DATA_TYPES` | none (5 storage classes and type affinity, [Datatypes](https://www.sqlite.org/datatype3.html)) | |
| `SHOW RESERVED_WORDS` | none in SQL. The C API has `sqlite3_keyword_name()`, and there is a [keyword list](https://www.sqlite.org/lang_keywords.html). | |
| `EXPORT TABLE` / `EXPORT VIEW` | `SELECT sql FROM sqlite_schema WHERE name = ?` | |
| `EXPLAIN` | `EXPLAIN QUERY PLAN …` | |
| `SHOW USERS`, `GROUPS`, `TABLE_PRIVILEGES`, `NODES`, `PIPELINES`, `PIPELINE_STATUS`, `GEOMETRY_COLUMNS` | none | |
| `sys.column_distributions` style stats | `sqlite_stat1` (after `ANALYZE`) | index-selectivity strings, not min/max |

One gotcha seen in testing: `CREATE TABLE r AS SELECT … FROM sqlite_schema` gets a row for `r` itself, with NULL columns, because the new table's schema row exists while the CTAS runs.

## 8. Open questions (need a real Ocient system)

1. Does `SELECT * FROM (SHOW TABLES)`, `WITH x AS (SHOW TABLES) …` or `CREATE TABLE r AS (SHOW TABLES)` parse? (Expected: no.)
2. Does CTAS from `information_schema.*` / `sys.*` work, and does it need explicit TimeKey or clustering clauses?
3. Are `information_schema` views RBAC-filtered the same way as their `SHOW` counterparts?
4. The actual column lists of each `SHOW` on the production version (compare with §2.1).
5. What does `SHOW STATS` / `SHOW STATISTICS` do (pyocient sends it as an update)?
6. Does the server accept `DESCRIBE TABLE`, and what columns does it return?
