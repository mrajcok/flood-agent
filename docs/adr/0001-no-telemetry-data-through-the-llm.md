# No telemetry data passes through the LLM

Flow records are sensitive, so the LLM never sees them. It writes a single read-only `SELECT`. The MCP tool validates it, wraps it in `CREATE TABLE … AS` under a name the tool chooses, and creates the Result Table in a separate writable results database attached next to the read-only Telemetry Database. The LLM gets back only Query Metadata. The Agent, not the LLM, reads Result Tables to build table and chart Displays, and charts are Vega-Lite specs that reference a Result Table by name, with inline values forbidden.

## Consequences

- The LLM cannot interpret results itself (e.g. "why did traffic spike?"). The user reads the Displays, and the LLM must not state values it has not seen.
- Every query costs a table write. Ocient supports CTAS, so the pattern carries over to production.
- Opening the Telemetry Database read-only and keeping Result Tables in a separate database means LLM-generated SQL cannot modify source data, even if validation is bypassed.
- One deliberate exception: `run_count_query` returns a single non-negative integer from a `COUNT` query straight to the LLM, with no Result Table, so "how many…?" questions don't need a table write and a Display. A count is an aggregate, not a Flow Record; the tool accepts only `SELECT COUNT(...) FROM …` (a bare COUNT as the sole select column) and rejects anything that isn't one row holding one non-negative integer. Residual risk: repeated counts with narrowing filters can probe for values by bisection, so this reduces what the LLM sees rather than proving it learns nothing.
- A SQL Cell's count falls under the same exception. A SQL Cell is run by the user, without the LLM, through the same query path; a query that is a bare count shows as a Count Display, and an AI Cell below is sent that count, the same single integer. If the query does not yield exactly one non-negative integer it is run as a normal query and makes a Result Table, so the LLM is sent only Query Metadata (table name, columns, types, row count), never rows.
- The output of a SHOW or DESCRIBE TABLE Cell is metadata about the database (table, column and privilege names, types), not Flow Records, so it may reach the LLM: an AI Cell below is sent up to about 100 of its rows, with a note when rows were left out. It never creates a table. No other SQL Cell output ever reaches the LLM.
