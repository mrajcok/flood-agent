# No telemetry data passes through the LLM

Flow records are sensitive, so the LLM never sees them. It writes a single read-only `SELECT`. The MCP tool validates it, wraps it in `CREATE TABLE … AS` under a name the tool chooses, and creates the Result Table in a separate writable results database attached next to the read-only Telemetry Database. The LLM gets back only Query Metadata. The Agent, not the LLM, reads Result Tables to build table and chart Displays, and charts are Vega-Lite specs that reference a Result Table by name, with inline values forbidden.

## Consequences

- The LLM cannot interpret results itself (e.g. "why did traffic spike?"). The user reads the Displays, and the LLM must not state values it has not seen.
- Every query costs a table write. Ocient supports CTAS, so the pattern carries over to production.
- Opening the Telemetry Database read-only and keeping Result Tables in a separate database means LLM-generated SQL cannot modify source data, even if validation is bypassed.
