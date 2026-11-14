# Saved Tables outlive their Notebook and Cell

Renaming a Result Table is how users say "keep this", so a renamed table becomes a Saved Table: it gets a 30-day Retention by default (instead of a Result Table's 1 day) and is never deleted along with the Notebook or Cell that created it. Deleting a Notebook or a Cell (of any kind) only offers to keep or delete its Result Tables (those still named `rt_*`, from any of its Runs); Saved Tables are deleted explicitly, one at a time, from the Tables dialog, which can also delete Lookup Tables and Result Tables. We dropped the old "delete all its tables" choice because it made it too easy to throw away a table the user had deliberately kept, and a reader might otherwise expect a Notebook's delete to clean up everything it created.

## Consequences

- A Saved Table's lifecycle is no longer tied to its Notebook, only to its Retention and to an explicit delete. The Cell and Run that created it are still recorded but no longer decide when it goes.
- Deleting a table from the Tables dialog leaves any Displays of it, in any Notebook, showing as expired, as when it ages off.
