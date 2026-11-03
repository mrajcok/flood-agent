# The results database is readable from every Conversation

Users reuse results across Conversations: a table built in one Conversation, or a list of IP addresses they pasted in, should be usable in the next. So every Conversation's queries may read every live Result Table and every Lookup Table in the results database, and every Conversation may show any live Result Table as a table or chart Display. This drops the per-Conversation read isolation that ADR 0002 and the query authorizer enforced. The boundary is now the deployment's single user (in production, that user's Ocient schema).

The Conversation that creates a Result Table still owns it for lifecycle. A Result Table expires its Retention after it was created (default 24 hours), and using it from another Conversation does not extend it. When a Conversation or one prompt/answer exchange is deleted, the user chooses to keep its Result Tables until they age off (the default), delete only the tables still named `rt_*`, or delete them all.

Lookup Tables are created by the web app from rows the user pastes, never by the LLM. Their names always start with `lookup_`, and they never expire. The authorizer decides what counts as a Lookup Table from the app database's list of registered Lookup Tables, not from the `lookup_` prefix, so a Result Table that happens to be named `lookup_…` gains nothing.

## Consequences

- The LLM still never sees rows (ADR 0001). Its system prompt lists only the current Conversation's own Result Tables. Any other table, whether a Lookup Table or another Conversation's Result Table, reaches the LLM only through `describe_table(name)` when the user names it, and that call returns kind, columns, types and row count. Creating a Lookup Table tells the LLM nothing.
- Users refer to tables by their SQL name, so each table Display shows that name. Renaming a Result Table renames the actual table (there is no separate display name) and updates saved Displays and Chart Specs. Names starting with `rt_` or `lookup_` are reserved.
- Deleting a Conversation can leave Result Tables with no Conversation behind. They stay queryable by name until they expire.
- Showing another Conversation's Result Table saves the Display with the Conversation that shows it, but ownership stays with the creator: deleting the showing Conversation never drops the table.
- A Result Table that another Conversation depends on can disappear when its Retention ends or its owner deletes it. That Conversation's Displays of it then show as expired.
