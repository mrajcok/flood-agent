# Flood Agent

Flood stores network telemetry (NetFlow/IPFIX). Flood Agent is a notebook-based analyst tool for exploring that telemetry and investigating volumetric attacks, without ever exposing telemetry data to the LLM.

## Actors

**Agent**:
The backend orchestrator that runs an AI Cell's LLM loop, executes the LLM's queries, and reads Result Tables to build Displays. The only component allowed to touch telemetry data.
_Avoid_: Bot, assistant (for this role)

**LLM**:
The language model reached through OpenRouter. It sees the Semantic Layer, the user's prompts and notes, and Query Metadata — never telemetry data.
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
A table created from a single query (an AI Cell's LLM-written query, or a SQL Cell's), holding that query's output so it never has to be returned to the LLM. It belongs to the Cell and Run that made it, and any Notebook can query it by name until it expires. It always has its generated name (`rt_<date>_<time>`); once the user renames it, it becomes a Saved Table.
_Avoid_: Temp table, query result, output table, `rt_` table

**Saved Table**:
A Result Table the user has renamed to keep it. It is queried by its new name, still has a Retention (30 days unless the user picks another), and is never deleted along with its Cell or Notebook; the user deletes it explicitly.
_Avoid_: Renamed Result Table, kept table, named table

**Lookup Table**:
A table the user creates by pasting rows or loading a file of them (e.g. a list of IP addresses and descriptions), for queries in any Notebook to join or filter against. Its contents never reach the LLM.
_Avoid_: Reference table, Result Table (for this)

**Retention**:
How long after its creation a Result Table or Saved Table is kept before it expires (default 1 day for a Result Table, 30 days for a Saved Table). The user can change it per table; using the table, or activity in any Notebook, does not extend it. Lookup Tables have no Retention.
_Avoid_: TTL (in user-facing text)

**Table Catalog**:
The record of every table a user can query (Telemetry Database tables, Result Tables, Saved Tables, Lookup Tables), with each one's kind and Retention. It is the only authority on whether a table is live, and on the rules for naming, renaming and deleting tables. Table names ignore case, and are kept in lower case.
_Avoid_: Registry, table manager

**Refresh**:
Running a Result Table's, Saved Table's or Count Display's stored query again, without the LLM, and replacing the old result with the new one under the same name. The user refreshes each table or count individually; there is no refresh-all. A refreshed table's Retention starts again. A Refresh is never a Run: it changes no cell's Run, and nothing goes stale. A SQL Cell has no Refresh control, because its Run is its refresh.
_Avoid_: Re-run, update, reload

**Query Metadata**:
What the LLM is told about a Result Table after a query runs: success or failure, column names and types, row count, and a value-free error message.
_Avoid_: Query result, response

## Notebook

**Notebook**:
An ordered list of Cells a user builds up to explore telemetry; it replaces the old Conversation. The Result Tables its Cells make belong to those Cells. Deleting a Notebook (or a single Cell) lets the user choose whether its Result Tables are deleted now or left to expire; its Saved Tables are always kept.
_Avoid_: Conversation, session, thread, chat

**Cell**:
One item of a Notebook: a Markdown Cell, a SQL Cell or an AI Cell.
_Avoid_: Block, step, turn

**Markdown Cell**:
A Cell of notes, rendered as Markdown, that can embed live table and chart Displays. Its text reaches the LLM only when Send to AI is ticked. It is never Run.
_Avoid_: Note cell, text cell

**SQL Cell**:
A Cell holding a SELECT or WITH query the user wrote, or a SHOW or DESCRIBE TABLE command picked from a drop-down, Run without the LLM. A query makes a Result Table (or, if it is a bare count, a Count Display); SHOW and DESCRIBE output is kept with the Run and makes no table. A SQL Cell is never stale.
_Avoid_: Query cell, code cell

**AI Cell**:
A Cell with a prompt and its own model, Effort and row cap. Running it asks the LLM, which streams an answer, Displays and a Trace.
_Avoid_: Prompt cell, chat cell

**Run**:
Executing a SQL Cell or an AI Cell. A Cell keeps only its latest Run, which is what it shows and what AI Cells below it see; a new Run replaces the old one. A Run is stopped, failed or ok, and records the settings it ran with. Retires "turn".
_Avoid_: Turn, execution, exchange

**Draft**:
A Cell's edited prompt, SQL or settings that have not been Run. A Cell with a Draft that differs from its latest Run shows its last output marked "edited, not run". The LLM never sees a Draft.
_Avoid_: Unsaved edit

**Stale**:
An AI Cell is stale when the context it would be sent now differs from the context its latest Run was sent, for instance because a Cell above it was edited and Run, moved or deleted. It is marked, never re-run automatically. A Refresh or a rename does not make a Cell stale.
_Avoid_: Out of date, dirty

**Send to AI**:
A Markdown Cell's checkbox, off by default, that includes its text in the context of the AI Cells below it.
_Avoid_: Share with LLM

**AI Cell context**:
What an AI Cell is sent when it runs: the system prompt, then the Cells above it in order, each as its latest Run left it (a ticked Markdown Cell's text, a SQL Cell's query with its Query Metadata, an AI Cell's prompt, answer and tool results), then its own prompt. It is built from what each Cell ran, never from Drafts, and never holds telemetry rows. Cells below are never sent.
_Avoid_: History, conversation

**Chart Spec**:
A declarative chart description written by the LLM that names a Result Table or Saved Table instead of containing data.
_Avoid_: Chart config, visualization

**Count**:
The single integer `run_count_query` returns to the LLM from a `SELECT COUNT(...)`. The only telemetry-derived value the LLM sees (ADR 0001), whether an AI Cell's query or a SQL Cell above it made it; no Result Table is created.
_Avoid_: Count result

**Trace**:
The per-Run record of an AI Cell of what passed between the Agent and the LLM — prompts, generated SQL, Query Metadata, errors, Chart Specs, cost and timings — shown to the user on demand. While the Cell runs, the same entries appear live, one section per LLM call, each collapsing when the next call starts; the live view is replaced by the Trace line in the Cell's footer when the Run ends.
A user can **stop** a running Cell: what it had streamed is kept (in the page, the Run and the Trace, marked stopped), and AI Cells below see it as stopped.
_Avoid_: Debug log, details

**Query Plan**:
The database's description of how it would run a query the LLM wrote, shown to the user on demand from a Trace. It is computed when asked, without the LLM, reads no rows, and is never sent to the LLM.
_Avoid_: Explain output, execution plan

**Display**:
A table or chart the Agent renders for the user from a Result Table or Saved Table.
_Avoid_: Widget, view, output

**Count Display**:
A single number from a count query, shown to the user with its title, the time it was computed and a Refresh control. It is saved with its Run and has no Retention.
_Avoid_: Count result, metric

## Fake Mode

**Fake Mode**:
The state of a prompt sent with the Model Choice "No model": a scripted stand-in answers instead of a language model, so no model is called and no API key is needed. Everything else — the Agent, queries, Result Tables, Displays, Traces — runs for real. A Notebook can mix Fake Mode and real AI Cells.
_Avoid_: Demo mode, offline mode, mock mode

**Canned Prompt**:
A fixed prompt offered in an AI Cell while "No model" is selected, always sent as a Fake Mode prompt, paired with the scripted LLM replies it produces.
_Avoid_: Sample query, test prompt, scenario

## Model choice

**Model Choice**:
The language model (or "No model", which is Fake Mode) the user picks for an AI Cell, from a curated list.
_Avoid_: Model picker, backend

**Effort**:
How much reasoning the user asks the chosen model to spend on an AI Cell's Run. Only some models support it.
_Avoid_: Reasoning level, thinking budget

**Context Usage**:
How much of the model's context window an AI Cell's context filled, as the provider's token count for the Run's latest LLM call against the window of the model the Cell ran with (e.g. 187k tokens of 1M). Shown in the Cell's footer so the user can tell when to start a new Notebook.
_Avoid_: Context size, token count, memory
