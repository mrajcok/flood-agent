# Download restriction follows column lineage

Some Telemetry Database columns identify a Subscriber or a device (the Restricted Columns, marked `restricted: true` in the Semantic Layer), and their values must be encrypted at rest. A query can carry them into a Result Table under any name, or through a chain of Result Tables. We therefore restrict a download by what a query's output carries, not by the names of its tables or columns: whenever a query creates or Refreshes a Result Table, CANOPY parses it with `sqlglot` (the postgres dialect), follows each output column back to the columns it takes its values from, and records the restricted ones in the Table Catalog. A table with a Restricted Column can only be downloaded masked or encrypted.

An output column is restricted when a restricted value reaches it through expressions, CASE, functions, aggregates, set operations, CTEs or subqueries, with two exceptions:

- **COUNT**, including `COUNT(DISTINCT …)`: a number of Subscribers identifies none. `MIN`, `MAX` and string aggregation return the values themselves, so they restrict.
- **Filter-only reads**: a column read only in WHERE, JOIN ON, GROUP BY, HAVING, PARTITION BY or ORDER BY chooses or orders rows without appearing in them.

It **fails closed**. If a table read has Restricted Columns and lineage cannot be worked out (the query is not accepted or does not parse, a table or column does not resolve, a construct is not followed, such as a recursive CTE, or it is not of the shapes below), the lineage is unknown: the query still runs, but its table can only be downloaded encrypted, and a query reading it counts every one of its columns as restricted. A query that reads no table with Restricted Columns needs no lineage, so unusual SQL over other tables is not penalised.

## Where the parser and the database differ

The parser is not the database, and any difference in how the two read a query is a way round the control: block comments that nest for one only, `date 'x'` (a typed literal to one, a column under an alias to SQLite), `current_user`, the case of a non-ASCII name. So the parser is never trusted with more than it has to be.

1. **SQLite says which tables are read.** The query is compiled on the connection that ran it, and the tables are those its program opens a cursor on, together with those the authorizer is asked about. Neither is enough alone: the authorizer is not asked about the columns of a `FULL JOIN … USING`. Whether lineage is needed at all is decided from this, with no parsing, so no difference in parsing can make a query that reaches restricted rows look unrestricted.
2. **The parser reads a rewritten query.** The query is tokenised by SQLite's lexing rules and written again (`sqlite_sql.py`): no comments, every name double-quoted, every literal replaced, every function but COUNT given one made-up name, every operator reduced to one that takes the same values. A word that SQLite does not reserve is taken for a keyword only where SQLite cannot be reading it as a name. Anything else is not accepted: backticks, square brackets, parameters, non-ASCII names, names the parser makes up for itself (`_col_0`), keywords and constructs not listed.
3. **The parse is checked against the database.** The lineage is unknown if SQLite reads a table, or a Restricted Column, that the parser does not see; if the output columns are not as many, or not in the order, SQLite made them; or if a table's columns are not those its schema gives, in that order.
4. **Names are followed only where both must resolve them alike.** How the two find what a name means differs in too many places to correct one at a time: SQLite does not show a FROM subquery or a CTE the sources beside it, so it takes `d.imsi` there from an outer `d`; it makes up the name `x:1` for a second column `x`; it takes `x IN name` for a table. So a query over restricted tables is followed only if it is of these shapes, and is unknown otherwise:
   - Each name of a table, CTE or source alias means one thing wherever it is written. An alias may be used again for the same table or CTE (`flows f` in both halves of a UNION), not for another, nor may it be the name of another table in the query. No two CTEs have one name, and none is used before it is defined.
   - No column is qualified with a database (`main.diameter.imsi`), and there is no `x IN table_name`.
   - A column without a qualifier belongs to a source of the query it is written in. Only in WHERE, GROUP BY, HAVING and ORDER BY may it instead be one of that query's own output names. A correlated subquery names an outer query's column with a qualifier.
   - A subquery in FROM or JOIN, and a CTE, refers to nothing outside its own sources.
   - Each column of a subquery in FROM or JOIN, and of a CTE, has a name of its own: a column's, an alias or one in the CTE's column list; no two alike (ignoring ASCII case), none with `:` in it, none `true` or `false`. Brackets around something that is not a query take no alias.
   - No NATURAL join, no RIGHT or FULL join with USING, and no `*` over any join with USING (the two name or order the columns differently).
   - No subquery in LIMIT, OFFSET or a window frame: there a value decides how many rows are returned or counted, so it can be read off the result.

**The rows read and the restriction go together.** Lineage is worked out inside the transaction that made the Result Table, whose lock stops any source being replaced meanwhile. While a table is being replaced and either its old or new rows are restricted, the Table Catalog records its lineage as unknown. A download checks the restriction and starts reading the rows while it holds the Table Catalog's lock, which a replacement, rename or deletion also takes, so no other table's rows can come to be under the name in between.

**At download time** lineage is worked out again from the table's stored query against the current Semantic Layer and Table Catalog, and any columns found are added to the recorded ones, so a newly restricted column does not stay downloadable from older tables. The live tables that query reads are worked out again in the same way, so this carries down a chain of Result Tables. It never makes a table less restricted than recorded.

**Only downloads are guarded.** Displays, charts, Copy to Markdown, Refresh and what the LLM is told behave as before; the LLM is told which columns are restricted, and still sees no rows (ADR 0001).

## Considered Options

- **A list of restricted tables.** Rejected: every table made from `diameter` would be restricted, including counts per hour and addresses filtered by IMSI, and a list of names says nothing about the Result Tables queries make.
- **The SQLite authorizer's read set alone.** Rejected: it reports every column a query reads, filter-only ones included, not which output column they reach. It is one of the checks on the parser (above), and the tests use it as an oracle for the columns lineage says a query reads.
- **Column security in Ocient.** Rejected: Ocient has none.

## Consequences

- Which output columns carry restricted values still comes from parsing. A difference in grammar that survives the rewriting and the checks would be missed; the tests compare lineage with the values queries really return, over every keyword of SQLite and every SQLite function.
- Some valid SQL over restricted tables has unknown lineage, and so downloads encrypted only: for example `COUNT(*)` over a subquery that selects `1`, or a correlated subquery that names the outer column without its qualifier.
- Ocient is not SQLite. Moving the Telemetry Database there needs its own rewriting rules and its own account of what a query reads; without them lineage over restricted tables must be unknown.
- A table whose source tables have expired keeps what was recorded when it was made or last Refreshed.
- Tables registered before lineage was recorded have unknown lineage.
