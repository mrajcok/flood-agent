"""The events a Cell Run streams to the page (and to harnesses such as the eval runner): text, Displays,
Trace entries, errors and the end. Shared by the Agent's AI Cell loop and SQL Cell Runs."""

from dataclasses import dataclass, field


@dataclass
class TextDelta:
    text: str


@dataclass
class DisplayEvent:
    kind: str
    result_table: str
    columns: list[str] = field(default_factory=list)
    spec: dict | None = None  # charts: Chart Spec after data-reference resolution
    source_spec: dict | None = None  # charts: Chart Spec as the LLM wrote it (for the Trace)


@dataclass
class CountEvent:
    """A Count Display: the number a successful run_count_query returned, shown as its own one-line item.
    `sql` is kept (also in the saved definition) so a later Refresh can run it again."""

    title: str
    value: int
    sql: str
    computed_at_ms: int
    display_id: int | None = None  # the saved Display, which a Refresh names


@dataclass
class TraceEntry:
    """Trace data for ticket #10 to pick up. `query_error` carries the raw SQLite error,
    which is never sent to the LLM, next to the sanitised error the LLM did see."""

    kind: str
    sql: str | None = None
    raw_error: str | None = None
    sanitized_error: str | None = None


@dataclass
class TraceReady:
    """The finished Trace for the Run (already persisted); for the UI only, never for the LLM."""

    trace: dict


@dataclass
class FlagEvent:
    """Tokens in a completed reply that the LLM could not have known. For the analyst only
    (reply note + Trace); never sent to the LLM."""

    tokens: list[str]
    items: list[dict] = field(default_factory=list)  # [{"type": "ip"|"number"|"domain", "token": ...}]


@dataclass
class LlmCallStarted:
    """The Run is about to make its `number`th LLM call (1-based); for the live view only."""

    number: int


@dataclass
class TraceLive:
    """A Trace entry, the moment it is added to the Run's Trace; for the live view only."""

    entry: dict


@dataclass
class TurnError:
    """The Run failed partway. `message` was already streamed to the user (and saved); `detail`
    is the technical description (also in the Trace) for harnesses such as the eval runner."""

    message: str
    detail: str


@dataclass
class TurnEnd:
    pass


Event = TextDelta | DisplayEvent | CountEvent | TraceEntry | FlagEvent | TraceReady | LlmCallStarted | TraceLive | TurnError | TurnEnd
