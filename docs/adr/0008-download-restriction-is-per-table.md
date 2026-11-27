# Download restriction is per table

Some Telemetry Database columns identify a Subscriber or a device (the Restricted Columns, marked `restricted: true` in the Semantic Layer), and their values must be encrypted at rest. A query can carry them into a Result Table under any name, or through a chain of Result Tables. We therefore restrict by table, not by column: a Result Table or Saved Table is a Restricted Table if its data came from any Telemetry table that has a Restricted Column, directly or through earlier results, whatever the query selected from it (a count, a filter or a join is enough). A Restricted Table can only be downloaded encrypted for the User's Public Key. Anything CANOPY cannot establish is restricted.

## The guarantee

**CANOPY never produces an unencrypted download of a table whose data came from a table with Restricted Columns.**

**It does not cover viewing.** Tables and charts are shown as before, and a signed-in User can select and copy what they see, Copy to Markdown Cell included. What the LLM is sent is unchanged (ADR 0001): it is told that such data can only be downloaded encrypted.

## What a query reads

SQLite says, and SQL is never parsed. The query is compiled again on the connection that ran it, and the tables read are those its program opens a cursor on, through the table or any of its indexes. The authorizer's reports only add to them. It cannot be relied on alone: on SQLite 3.46.0 it reports no read of the join column in `USING` and `NATURAL` joins.

What is recognised is an allow-list. Anything that cannot be mapped to a known Telemetry table or a registered table of this User (an unfamiliar cursor kind, a virtual table or table function, a schema table, an unregistered results table, a failure while establishing it) makes the sources unknown. The query still runs.

A table that SQLite removes from the compiled query and never reports, as it can `d` in `SELECT DISTINCT f.x FROM f LEFT JOIN d USING (k)`, is not a source: it cannot affect the result.

## The record

Each Result Table and Saved Table records in the Table Catalog the Telemetry tables its data came from: each Telemetry table its query read, and the recorded set of each Result or Saved Table it read. A Lookup Table contributes nothing. If any contribution is unknown, the whole record is unknown, which is not the same as no sources. The record is written by every path that makes rows (SQL Cell Runs, AI Cell queries and Refresh) and is never recomputed from stored SQL, so it outlives the tables it came from.

**A name has one record.** A query's new table is never given a name the Table Catalog holds, for a live table or an expired one: the name is chosen while the query holds the write lock on the results database, so no table can be made, renamed or dropped meanwhile. Registering a held name is refused, and leaves that name's record unknown.

**The rows read and the record go together.** The record is established inside the transaction that made the rows, whose lock stops any source being replaced meanwhile. While a table is being replaced, its visible record covers both the old rows and the new ones. A download checks the record and starts reading the rows while it holds the Table Catalog's lock, which a replacement, rename or deletion also takes, so no other table's rows can come to be under the name in between.

## At download time

A table is restricted if its record is unknown, names a Telemetry table that has a Restricted Column now, or names one the Semantic Layer does not describe. The check is against the configuration as it is at each download: marking a column restricted covers tables already made from its table, unmarking a table's last Restricted Column lets them download plain, and removing a table's description never unlocks data. Each encrypted download of a Restricted Table is logged with the User, table, source tables and key fingerprint.

## Considered Options

- **Column lineage by a SQL parser** (which output columns carry restricted values, so that other results download plain, and these masked or encrypted). Rejected: it is only as good as the parser's agreement with the database, and any difference in how the two read a query lets restricted values out unencrypted. Reviews found several, and there is no way to show that none are left.
- **The SQLite authorizer's column reads** (restrict a table only if its query read a Restricted Column). Rejected: they miss the join column of `USING` and `NATURAL` joins, and they report every column read, filter-only ones included, not which output column it reaches.
- **Masking** restricted values in an unencrypted download. Rejected: it needs to know which values are restricted, which only column lineage or recognising the values themselves could say, and either can miss some.
- **Column security in Ocient.** Rejected: Ocient has none.

## Consequences

- Flexibility is traded for safety: any query that reads `diameter`, even a count by command code, downloads encrypted only, as does the `flows_subscriber_join` metric.
- Lookup Tables are not inspected, so identifiers a User pasted into one do not restrict what is queried from it alone.
- Tables registered before sources were recorded have an unknown record, so they download encrypted only. A Refresh records the new rows' sources.
- Ocient is not SQLite. The same rule can use Ocient's record of the tables each query read; until that exists, the sources of results there are unknown.
