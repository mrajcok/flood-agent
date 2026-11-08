# Flood Agent

Flood stores network telemetry (NetFlow/IPFIX). Flood Agent is a chat-based analyst tool for exploring that telemetry and investigating volumetric attacks, without ever exposing telemetry data to the LLM.

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
A table created from a single LLM-written query in a Conversation, holding that query's output so it never has to be returned to the LLM. Any Conversation can query it by name until it expires. It always has its generated name (`rt_<date>_<time>`); once the user renames it, it becomes a Saved Table.
_Avoid_: Temp table, query result, output table, `rt_` table

**Saved Table**:
A Result Table the user has renamed to keep it. It is queried by its new name, still has a Retention (30 days unless the user picks another), and is never deleted along with its Conversation; the user deletes it explicitly.
_Avoid_: Renamed Result Table, kept table, named table

**Lookup Table**:
A table the user creates by pasting rows or loading a file of them (e.g. a list of IP addresses and descriptions), for queries in any Conversation to join or filter against. Its contents never reach the LLM.
_Avoid_: Reference table, Result Table (for this)

**Retention**:
How long after its creation a Result Table or Saved Table is kept before it expires (default 1 day for a Result Table, 30 days for a Saved Table). The user can change it per table; using the table, or activity in any Conversation, does not extend it. Lookup Tables have no Retention.
_Avoid_: TTL (in user-facing text)

**Table Catalog**:
The record of every table a user can query (Telemetry Database tables, Result Tables, Saved Tables, Lookup Tables), with each one's kind and Retention. It is the only authority on whether a table is live, and on the rules for naming, renaming and deleting tables. Table names ignore case, and are kept in lower case.
_Avoid_: Registry, table manager

**Query Metadata**:
What the LLM is told about a Result Table after a query runs: success or failure, column names and types, row count, and a value-free error message.
_Avoid_: Query result, response

## Conversation

**Conversation**:
One chat thread between a user and Flood Agent; the Result Tables it creates belong to it. Deleting it lets the user choose whether its Result Tables are deleted now or left to expire; its Saved Tables are always kept.
_Avoid_: Session, thread

**Chart Spec**:
A declarative chart description written by the LLM that names a Result Table or Saved Table instead of containing data.
_Avoid_: Chart config, visualization

**Count**:
The single integer `run_count_query` returns to the LLM from a `SELECT COUNT(...)`. The only telemetry-derived value the LLM sees (ADR 0001); no Result Table is created.
_Avoid_: Count result

**Trace**:
The per-turn record of what passed between the Agent and the LLM — prompts, generated SQL, Query Metadata, errors, Chart Specs, cost and timings — shown to the user on demand. While a turn runs, the same entries appear live, one section per LLM call, each collapsing when the next call starts; the whole live view is replaced by the one Trace line when the turn ends.
A user can **stop** a running turn: what it had streamed is kept (in the page, the history and the Trace, marked stopped), and the next prompt sees it.
_Avoid_: Debug log, details

**Display**:
A table or chart the Agent renders for the user from a Result Table or Saved Table.
_Avoid_: Widget, view, output

## Fake Mode

**Fake Mode**:
The state of a prompt sent with the Model Choice "No model": a scripted stand-in answers instead of a language model, so no model is called and no API key is needed. Everything else — the Agent, queries, Result Tables, Displays, Traces — runs for real. A Conversation can mix Fake Mode and real prompts.
_Avoid_: Demo mode, offline mode, mock mode

**Canned Prompt**:
A fixed user message offered while "No model" is selected, always sent as a Fake Mode prompt, paired with the scripted LLM replies it produces.
_Avoid_: Sample query, test prompt, scenario

## Model choice

**Model Choice**:
The language model (or "No model", which is Fake Mode) the user picks for their next prompt, from a curated list.
_Avoid_: Model picker, backend

**Effort**:
How much reasoning the user asks the chosen model to spend on a prompt. Only some models support it.
_Avoid_: Reasoning level, thinking budget

**Context Usage**:
How much of the chosen model's context window the Conversation occupies, as the provider's token count for the latest LLM call against the window size (e.g. 187k tokens of 1M). Shown so the user can tell when to start a new Conversation.
_Avoid_: Context size, token count, memory
