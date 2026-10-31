# Flood Defender

Flood stores network telemetry (NetFlow/IPFIX). Flood Defender is a chat-based analyst tool for exploring that telemetry and investigating volumetric attacks, without ever exposing telemetry data to the LLM.

## Actors

**Agent**:
The backend orchestrator that runs the conversation loop, executes the LLM's queries, and reads Result Tables to build Displays. The only component allowed to touch telemetry data.
_Avoid_: Bot, assistant (for this role)

**LLM**:
The language model reached through OpenRouter. It sees the Semantic Layer, user messages, and Query Metadata — never telemetry data.
_Avoid_: Agent (for the model)

## Data

**Telemetry Database**:
The read-only store of flow records (SQLite here, Ocient in production).
_Avoid_: Source DB, flow DB

**Flow Record**:
One NetFlow/IPFIX record summarising traffic between two endpoints over an interval.
_Avoid_: Flow (ambiguous), packet, event

**Circuit**:
An opaque 12-character identifier for the monitored link a Flow Record was observed on.
_Avoid_: Link, interface, port

**Time Key**:
The column flow records are partitioned on in Ocient (`etime`, in 15-minute buckets). Time-range filters belong on it.
_Avoid_: Timestamp (ambiguous between stime and etime)

**Semantic Layer**:
The curated description of the Telemetry Database — tables, columns, units, enums, example values, metrics and SQL dialect — that the LLM writes queries against.
_Avoid_: Schema (too narrow), data dictionary

**Result Table**:
A table created from a single LLM-written query, scoped to one Conversation, holding that query's output so it never has to be returned to the LLM.
_Avoid_: Temp table, query result, output table

**Query Metadata**:
What the LLM is told about a Result Table after a query runs: success or failure, column names and types, row count, and a value-free error message.
_Avoid_: Query result, response

## Conversation

**Conversation**:
One chat thread between a user and Flood Defender; owns its Result Tables.
_Avoid_: Session, thread

**Chart Spec**:
A declarative chart description written by the LLM that names a Result Table instead of containing data.
_Avoid_: Chart config, visualization

**Trace**:
The per-turn record of what passed between the Agent and the LLM — prompts, generated SQL, Query Metadata, errors, Chart Specs, cost and timings — shown to the user on demand.
_Avoid_: Debug log, details

**Display**:
A table or chart the Agent renders for the user from a Result Table.
_Avoid_: Widget, view, output
