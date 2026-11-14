# Tables are refreshed one at a time, by the user

A Result Table, Saved Table or count can be refreshed: its stored query is run again, without the LLM, and the result replaces the old one. Each table or count has its own refresh control, and that is all there is. There is no "refresh all tables in this Notebook", per-Run refresh, or page-level refresh. Deciding which queries are worth re-running (a fixed time range gains nothing; one using `now()` does) is the hard part, and the user already knows which data looks stale, so the user decides. If users ask for a bulk refresh we revisit this.

## Considered Options

- A page-level or per-Run "re-run queries" button that picks the relative-time queries itself. Rejected: it needs rules for which queries qualify, for the order of queries that read other Result Tables, and for what a failure part-way means.
- Replace by deleting the old rows and inserting new ones. Rejected: Ocient's `DELETE` does not give the disk space back without further system operations and is documented for small row sets, not whole tables (`docs/research/ocient-table-replacement.md`).

## Consequences

- A Refresh is never a Run. It changes no Cell's Run record or context fingerprint, so nothing goes stale and nothing below is re-sent. An AI Cell's tables and Count Displays keep their refresh controls, as do the Tables dialog and a Display of a table that another Notebook owns.
- A SQL Cell's Run is its refresh: running it again replaces its table under the same name with the same build-then-swap, and a failure or Stop leaves the old table as it was. So a SQL Cell's output has no separate refresh control.

- Refresh replaces the whole table by name, never row by row. The new rows are built under a temporary name first and swapped in, so a failing or timed-out query leaves the old table untouched. On SQLite the swap is one transaction; on Ocient it is a drop and rename (or one `CREATE OR REPLACE`, if testing shows that is atomic: see the research note). Until those tests are run, the Ocient behaviour is unverified.
- A table's Retention restarts when it is refreshed. A table that has already expired cannot be refreshed; the user asks again.
- The LLM is not told about a refresh (ADR 0001), so a refresh costs no tokens and leaves the cached system prompt alone. Numbers the LLM wrote earlier stay as written.
- The query and its row cap are kept with the table when it is created, so tables created before this change have no refresh control.
- Any Notebook that shows a table can refresh it (ADR 0003). The one shared table changes for everyone; ownership for lifecycle stays with its creator.
- A count becomes a displayed object (a Count Display) so it has something to refresh. It is saved with its Run and has no Retention.
