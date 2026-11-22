"""The tool outcome codec: what an MCP tool call produced, as one typed value.

The MCP server `encode`s an outcome to the text it returns; the Agent `decode`s that text once and
branches on the outcome's kind. `project` builds what the LLM is sent (the Query Metadata JSON), field by
field from each kind's allow-list: a field no projection names never reaches the LLM (ADR 0001). Values
only the Agent needs (`raw_error`, `executed_sql`, the raw text of unreadable content) live in a separate
`internal` part of the wire format that no projection reads; they reach only the Trace.
"""
import json
from dataclasses import dataclass, fields
from typing import Any

UNREADABLE = "The tool returned an unreadable result."


@dataclass(frozen=True)
class ResultTableMade:
    """A query made a Result Table. `truncated_to` and `note` are set together, only when rows were cut."""
    result_table: str
    columns: list
    row_count: int
    truncated_to: int | None = None
    note: str | None = None
    executed_sql: str | None = None  # internal: the CTAS that ran


@dataclass(frozen=True)
class Counted:
    count: int


@dataclass(frozen=True)
class TableDescribed:
    name: str
    kind: str
    columns: list
    row_count: int


@dataclass(frozen=True)
class DisplayMade:
    """A table or chart Display, with an optional note for the LLM."""
    display: dict
    note: str | None = None


@dataclass(frozen=True)
class Failed:
    """`error` is value-free and safe for the LLM. The rest is internal (Trace only)."""
    error: str
    raw_error: str | None = None
    executed_sql: str | None = None
    raw_content: str | None = None  # the undecodable text, when that is why this failed


@dataclass(frozen=True)
class Stopped:
    """The user's Stop ended the call (`from_query_result` makes it from a stopped query)."""
    error: str = "Stopped by the user."
    raw_error: str | None = None
    executed_sql: str | None = None


Outcome = ResultTableMade | Counted | TableDescribed | DisplayMade | Failed | Stopped

_INTERNAL = {"raw_error", "executed_sql", "raw_content"}
_KINDS: dict[str, type] = {
    "result_table_made": ResultTableMade, "counted": Counted, "table_described": TableDescribed,
    "display_made": DisplayMade, "failed": Failed, "stopped": Stopped,
}
_KIND_OF = {cls: kind for kind, cls in _KINDS.items()}


def encode(outcome: Outcome) -> str:
    """The text an MCP tool returns for this outcome."""
    public, internal = {}, {}
    for f in fields(outcome):
        value = getattr(outcome, f.name)
        if value is None:
            continue
        (internal if f.name in _INTERNAL else public)[f.name] = value
    return json.dumps({"outcome": _KIND_OF[type(outcome)], **public, "internal": internal})


def decode(text: str) -> Outcome:
    """The outcome in tool content; content that is not one becomes a generic Failed keeping the raw text."""
    try:
        return _decode(text)
    except (ValueError, TypeError, KeyError):
        return Failed(UNREADABLE, raw_content=text)


_TYPES: dict[str, Any] = {
    "result_table": str, "columns": list, "row_count": int, "truncated_to": int, "note": str, "count": int,
    "name": str, "kind": str, "display": dict, "error": str, "raw_error": str, "executed_sql": str,
    "raw_content": str,
}


def _decode(text: str) -> Outcome:
    wire = json.loads(text)
    cls = _KINDS[wire["outcome"]]
    internal = wire.get("internal", {})
    if not isinstance(internal, dict):
        raise TypeError("internal")
    values = {}
    for f in fields(cls):
        source = internal if f.name in _INTERNAL else wire
        if f.name in source:
            value = source[f.name]
            expected = _TYPES[f.name]
            if not isinstance(value, expected) or (expected is int and isinstance(value, bool)):
                raise TypeError(f.name)
            values[f.name] = value
    result = cls(**values)  # a missing required field raises TypeError
    if isinstance(result, DisplayMade):
        _project_display(result.display)  # a Display that cannot be projected is unreadable
    return result


def from_query_result(result: dict) -> Outcome:
    """The outcome of a `queries.run_query` / `run_count_query` result dict. Only the fields a kind names are
    copied, so a stray key in the dict goes nowhere."""
    if result.get("success"):
        if "count" in result:
            return Counted(result["count"])
        return ResultTableMade(
            result["result_table"], result["columns"], result["row_count"],
            result.get("truncated_to"), result.get("note"), result.get("executed_sql"),
        )
    kind = Stopped if result.get("stopped") else Failed
    return kind(result["error"], result.get("raw_error"), result.get("executed_sql"))


def project(outcome: Outcome) -> str:
    """What the LLM is sent for this outcome: built field by field, never by deleting keys from a dict.
    Same keys, order and serialisation as the Query Metadata the tools returned before the codec."""
    match outcome:
        case ResultTableMade():
            view: dict = {"success": True, "result_table": outcome.result_table, "columns": outcome.columns,
                          "row_count": outcome.row_count}
            if outcome.truncated_to is not None:
                view |= {"truncated_to": outcome.truncated_to, "note": outcome.note}
        case Counted():
            view = {"success": True, "count": outcome.count}
        case TableDescribed():
            view = {"success": True, "name": outcome.name, "kind": outcome.kind, "columns": outcome.columns,
                    "row_count": outcome.row_count}
        case DisplayMade():
            view = {"success": True, "display": _project_display(outcome.display)}
            if outcome.note is not None:
                view["note"] = outcome.note
        case Stopped():
            view = {"success": False, "stopped": True, "error": outcome.error}
        case Failed():
            view = {"success": False, "error": outcome.error}
    return json.dumps(view)


def _project_display(display: dict) -> dict:
    """A table Display names its columns; a chart Display echoes its Chart Spec (as before)."""
    if display["kind"] == "chart":
        return {"kind": "chart", "result_table": display["result_table"], "spec": display["spec"]}
    return {"kind": display["kind"], "result_table": display["result_table"], "columns": display["columns"]}
