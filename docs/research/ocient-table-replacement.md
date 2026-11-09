# Replacing a Result Table on Ocient (refresh)

Research for the per-table **Refresh** feature (see ADR 0006). Researched 2026-11-08 from the Ocient docs, read through a summarising fetch tool, so quotes are close to, not exactly, the page text. **Nothing here has been run against a real Ocient system.** The test plan in section 3 is not done; its results table is empty on purpose.

## 1. What the docs say

Sources: [Tables reference](https://docs.ocient.com/tables.md) (CREATE TABLE, CTAS, DROP TABLE, ALTER TABLE RENAME), [DML reference](https://docs.ocient.com/data-manipulation-language-dml-statement-reference.md), [Remove Records from an Ocient System](https://docs.ocient.com/remove-records-from-an-ocient-system).

- **Replace in one statement.** `CREATE [ OR REPLACE ] TABLE [ IF NOT EXISTS ] table_name … [ AS (query) ]`. So `OR REPLACE` applies to CTAS. "Using `REPLACE` requires DELETE privileges." The name must be unique "unless the `REPLACE` keyword is specified".
- **CTAS does not inherit keys.** "CTAS does not inherit the TimeKey, clustering key, clustering indexes, or secondary indexes from the source table. You must specify them explicitly." The TimeKey must be a timestamp, date or time column with a bucket resolution; clustering-key columns must be fixed-length and not nullable. CTAS also "does not support default values and explicit nullable definitions". ([details](https://docs.ocient.com/timekeys-and-clustering-keys.md))
- **DROP TABLE.** Removes the table and its views; cannot be undone. `IF EXISTS` turns a missing table into a warning. Needs system-level rights or DELETE TABLE privilege. The page says nothing about speed, asynchrony or space reclamation.
- **ALTER TABLE … RENAME TO.** `ALTER TABLE [ IF EXISTS ] old RENAME TO new`; needs ALTER TABLE privilege. Running pipelines stay bound to the original identifier.
- **DELETE is the wrong tool for replacing a table.** It "does not restore the disk space of deleted rows without further system operations"; "`DELETE` is a good option when removing small row sets, not entire tables". Concurrent deletes on a table "might impact performance". It rolls back on failure. TRUNCATE is the documented way to remove all or some records (including by `segment_group_id`) and keep the schema; its own syntax page was not found.

## 2. What the docs do not say (the open questions)

1. Is `CREATE OR REPLACE TABLE … AS (query)` atomic? If the query fails or is cancelled midway, does the old table survive with its rows?
2. How is `OR REPLACE` implemented and what does it cost on a large table (drop-and-create, or something cheaper)? Is space reclaimed promptly?
3. Is DDL transactional, so that a drop plus rename could be made all-or-nothing?
4. What does a reader querying the table during a replace, or during the drop-then-rename gap, see: the old rows, the new rows, or an error?

## 3. Test plan (not yet run)

Run on a scratch Ocient database with a user holding the privileges the app's user will have. Use a Result-Table-sized source (say 1M rows) and a second, tiny one. Record observed behaviour and timings in the results table; do not infer.

| # | Test | Answers | Result |
|---|---|---|---|
| T1 | `CREATE OR REPLACE TABLE t AS (SELECT …)` over an existing `t`, query succeeds. Time it. | Q2 cost; replaced contents correct? | not run |
| T2 | Same, with a query that fails at run time (bad cast, missing table). Does `t` still exist with its old rows? | Q1 atomicity (decides whether the one-statement refresh is safe) | not run |
| T3 | Same, with a query cancelled by the app's timeout part-way. | Q1 | not run |
| T4 | A reader running `SELECT COUNT(*) FROM t` while T1 runs on a large `t`. | Q4 | not run |
| T5 | Build-then-swap: `CREATE TABLE t_new AS (…)`; `DROP TABLE t`; `ALTER TABLE t_new RENAME TO t`. Time each step; a reader polling `t` throughout: how long is it missing? | Q3, Q4: the fallback design | not run |
| T6 | `DROP TABLE` on a large table: returns at once? Is disk space reclaimed, and when? | Q2 | not run |
| T7 | CTAS with no TimeKey/clustering key. Does it fail? What minimal explicit DDL do Result Tables need? | the production CTAS caveat, whatever refresh does | not run |
| T8 | After `RENAME TO`, are data, privileges and views intact? | swap correctness | not run |
| T9 | The privileges the app's database user needs for create, replace, drop and rename. | deployment | not run |

## 4. Decision that does not wait for the tests

Refresh replaces the whole table by name and never deletes rows (ADR 0006). It is built as build-then-swap: the new rows are built under a temporary name first, so a failing or timed-out query can never touch the existing table. On SQLite the swap is one transaction. On Ocient, T1–T5 decide whether the swap can be a single `CREATE OR REPLACE` (if T2 and T3 show the old table survives) or must stay drop-then-rename (accepting the short gap measured in T5).
