# The results database is readable from every Notebook

Users reuse results across Notebooks: a table built in one Notebook, or a list of IP addresses they pasted in, should be usable in the next. So every Notebook's queries (an AI Cell's or a SQL Cell's) may read every live Result Table and every Lookup Table in the results database, and every Notebook may show any live Result Table as a table or chart Display. This drops the per-Conversation read isolation that ADR 0002 and the query authorizer once enforced (Conversations are now Notebooks). The boundary is now the deployment's single user (in production, that user's Ocient schema).

The Cell and Run that create a Result Table still own it for lifecycle: a table records the Cell and Run that made it, and a table is from the current Run when its Run is the Cell's latest. A Result Table expires its Retention after it was created (default 24 hours), and using it from another Notebook does not extend it. When a Notebook or one Cell is deleted, the user chooses to keep its Result Tables until they age off (the default) or delete the tables still named `rt_*`. (ADR 0004: renamed tables are Saved Tables, which are never deleted with their Notebook or Cell.)

Lookup Tables are created by the web app from rows the user pastes, never by the LLM. Their names always start with `lookup_`, and they never expire. The authorizer decides what counts as a Lookup Table from the app database's list of registered Lookup Tables, not from the `lookup_` prefix, so a Result Table that happens to be named `lookup_…` gains nothing.

## Consequences

- The LLM still never sees rows (ADR 0001). An AI Cell's system prompt lists only the live Result Tables of the Cells above it (their current Runs) and its own earlier in the current Run. Any other table, whether a Lookup Table or another Notebook's Result Table, reaches the LLM only through `describe_table(name)` when the user names it, and that call returns kind, columns, types and row count. Creating a Lookup Table tells the LLM nothing.
- Users refer to tables by their SQL name, so each table Display shows that name. Renaming a Result Table renames the actual table (there is no separate display name) and updates saved Displays and Chart Specs. Names starting with `rt_` or `lookup_` are reserved.
- Deleting a Notebook can leave Result Tables with no Notebook behind. They stay queryable by name until they expire.
- Showing another Notebook's Result Table saves the Display with the Cell that shows it, but ownership stays with the creator: deleting the showing Notebook or Cell never drops the table.
- A Result Table that another Notebook depends on can disappear when its Retention ends or its owner deletes it. That Notebook's Displays of it then show as expired.
