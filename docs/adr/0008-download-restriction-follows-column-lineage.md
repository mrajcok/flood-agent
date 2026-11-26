# Download restriction follows column lineage

Some Telemetry Database columns identify a Subscriber or a device (the Restricted Columns, marked `restricted: true` in the Semantic Layer), and their values must be encrypted at rest. A query can carry them into a Result Table under any name, or through a chain of Result Tables. We therefore restrict a download by what a query's output carries, not by the names of its tables or columns: whenever a query creates or Refreshes a Result Table, CANOPY parses it with `sqlglot` (the postgres dialect, for SQLite and Ocient alike), follows each output column back to the columns it takes its values from, and records the restricted ones in the Table Catalog. A table with a Restricted Column can only be downloaded masked or encrypted.

An output column is restricted when a restricted value reaches it through expressions, CASE, functions, aggregates, set operations, CTEs or subqueries, with two exceptions:

- **COUNT**, including `COUNT(DISTINCT …)`: a number of Subscribers identifies none. `MIN`, `MAX` and string aggregation return the values themselves, so they restrict.
- **Filter-only reads**: a column read only in WHERE, JOIN ON, GROUP BY, HAVING, PARTITION BY or ORDER BY chooses or orders rows without appearing in them.

It **fails closed**. If lineage cannot be worked out (the query does not parse, a table or column does not resolve, a construct is not followed, such as a recursive CTE) and a table read has Restricted Columns, or the tables read cannot even be listed, the lineage is unknown: the query still runs, but its table can only be downloaded encrypted, and a query reading it counts every one of its columns as restricted. A query that reads no table with Restricted Columns needs no lineage, so unusual SQL over other tables is not penalised.

**At download time** lineage is worked out again from the table's stored query against the current Semantic Layer and Table Catalog, and any columns found are added to the recorded ones, so a newly restricted column does not stay downloadable from older tables. The live tables that query reads are worked out again in the same way, so this carries down a chain of Result Tables. It never makes a table less restricted than recorded.

**Only downloads are guarded.** Displays, charts, Copy to Markdown, Refresh and what the LLM is told behave as before; the LLM is told which columns are restricted, and still sees no rows (ADR 0001).

## Considered Options

- **A list of restricted tables.** Rejected: every table made from `diameter` would be restricted, including counts per hour and addresses filtered by IMSI, and a list of names says nothing about the Result Tables queries make.
- **The SQLite authorizer's read set.** Rejected: it reports every column a query reads, filter-only ones included, not which output column they reach, and Ocient has no equivalent. The tests use it as an oracle for the columns lineage says a query reads.
- **Column security in Ocient.** Rejected: Ocient has none.

## Consequences

- Lineage comes from parsing, not from the database engine, so wherever the parser and the database could read a query differently it has to be treated as unknown.
- A table whose source tables have expired keeps what was recorded when it was made or last Refreshed.
- Tables registered before lineage was recorded have unknown lineage.
