"""FastAPI adapter: server-rendered Jinja + htmx + Alpine, streaming over SSE.

A thin layer over `Agent.run_turn`. The row-JSON endpoint is the Agent's internal data
path to the browser; it is a plain HTTP route, never registered as an MCP tool.
"""

import csv
import io
import json
import sqlite3
import time
import uuid
from contextlib import aclosing, asynccontextmanager
from dataclasses import dataclass, field
from html import escape
from pathlib import Path
from typing import AsyncGenerator, Literal, cast

from fastapi import FastAPI, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from flood_agent import dev_tools, lookup_tables, queries
from flood_agent.agent import (
    NO_MODEL,
    STOPPED_NOTE,
    Agent,
    Conversation,
    CountEvent,
    DisplayEvent,
    FlagEvent,
    LlmCallStarted,
    NotebookBusy,
    NotRefreshable,
    RefreshFailed,
    RefreshInProgress,
    TablesChoice,
    TextDelta,
    TraceLive,
    TraceReady,
    TurnEnd,
    resolve_chart_data,
)
from flood_agent.catalog import TableExpired
from flood_agent.context_usage import context_usage
from flood_agent.lookup_tables import InvalidLookupTable
from flood_agent.notebooks import DEFAULT_MAX_ROWS, MARKDOWN
from flood_agent.queries import InvalidSort
from flood_agent.show_commands import DESCRIBE_TABLE, SHOW_COMMANDS, SHOW_IN_TEXTBOX, is_show_statement
from flood_agent.store import COUNT_KIND
from flood_agent.trace import context_tokens, short_model, split_system_prompt

TEMPLATES = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))
TEMPLATES.env.filters["split_system_prompt"] = split_system_prompt
TEMPLATES.env.filters["short_model"] = short_model
STATIC_DIR = Path(__file__).parent / "static"


def static_version(name: str) -> int:
    """Changes whenever the static file does, so a browser never reuses a stale cached copy."""
    return (STATIC_DIR / name).stat().st_mtime_ns


TEMPLATES.env.globals["static_version"] = static_version
TEMPLATES.env.globals["show_commands"] = SHOW_COMMANDS


def _sse(event: str, data: str) -> str:
    lines = "".join(f"data: {line}\n" for line in data.split("\n"))
    return f"event: {event}\n{lines}\n"


@dataclass(frozen=True)
class PickerEntry:
    """One clickable prompt in the Fake Mode picker, with the follow-ups listed under it."""

    prompt: str
    fails_on_purpose: bool = False
    follow_ups: list["PickerEntry"] = field(default_factory=list)


@dataclass
class LiveCounter:
    """The number of the LLM call a streaming turn or cell is in, for the live entries."""

    call: int = 0


NOTEBOOK_COUNT_REFRESH = "/api/notebook-displays/{}/refresh"  # a notebook Count Display's id is not a Conversation's


def live_event_sse(ev, key: str, live: LiveCounter, refreshable_counts: bool = True, notebook: bool = False) -> str | None:
    """The SSE message a streamed event becomes in the live view (text, Displays, steps, flags), shared by
    Conversation turns and Notebook cells; None for the events a caller handles itself (Trace, end).
    `key` names the turn or cell in the ids of its live steps. Count Displays are only refreshable
    (display_id); `notebook` says it is a cell's, which refreshes through the notebook endpoint."""
    if isinstance(ev, TextDelta):
        return _sse("text", f'<span class="md-delta">{escape(ev.text)}</span>')  # markdown.js renders it
    if isinstance(ev, CountEvent):
        return _sse("display", TEMPLATES.get_template("display_count.html").render(
            title=ev.title, value=ev.value, computed_at_ms=ev.computed_at_ms,
            display_id=ev.display_id if refreshable_counts else None,
            refresh_url=NOTEBOOK_COUNT_REFRESH.format(ev.display_id) if notebook and ev.display_id else None).strip())
    if isinstance(ev, DisplayEvent) and ev.kind == "chart":
        html = TEMPLATES.get_template("display_chart.html").render(
            spec_json=json.dumps(ev.spec), **_table_display_urls(ev.result_table)
        ).strip()  # no stray newlines: the reply box keeps whitespace
        return _sse("display", html)
    if isinstance(ev, DisplayEvent):
        html = TEMPLATES.get_template("display_table.html").render(
            **_table_display_urls(ev.result_table), result_table=ev.result_table, columns=ev.columns,
        ).strip()
        return _sse("display", html)
    if isinstance(ev, LlmCallStarted):
        live.call = ev.number
        return _sse("live-step", TEMPLATES.get_template("live_step.html").render(number=ev.number, turn_id=key).strip())
    if isinstance(ev, TraceLive):
        entry_html = TEMPLATES.get_template("trace_entry.html").render(e=ev.entry, step=live.call).strip()
        oob = f'<div hx-swap-oob="beforeend:#live-body-{key}-{live.call}">{entry_html}</div>'
        if ev.entry["kind"] == "llm_step":  # the call is over: its summary line gets the figures
            meta = TEMPLATES.get_template("live_meta.html").render(e=ev.entry).strip()
            oob += f'<span hx-swap-oob="innerHTML:#live-meta-{key}-{live.call}">{meta}</span>'
        return _sse("entry", oob)
    if isinstance(ev, FlagEvent):
        return _sse("flag", TEMPLATES.get_template("flag_note.html").render(flags=ev.items))
    return None


def _table_display_urls(result_table: str) -> dict:
    """The URLs a table Display (display_table.html) loads and saves through."""
    base = f"/api/results/{result_table}"
    return {"rows_url": f"{base}/rows", "psv_url": f"{base}/psv", "settings_url": f"{base}/settings"}


NEEDS_KEY = "needs OPENROUTER_API_KEY"


def create_app(
    agent: Agent, picker: list[PickerEntry] | None = None, has_api_key: bool = True, dev: bool = False
) -> FastAPI:
    """The Model dropdown lists the curated models, disabled when `has_api_key` is False, then "No model"
    (Fake Mode, always selectable). The `picker` Canned Prompts show on the page only while it is selected.
    `dev` (set by flood-agent-dev only) adds the button and endpoint that shift the Telemetry Database to now."""
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        # One long-lived MCP server for the app; a server that can't start fails app startup.
        async with agent:
            yield

    app = FastAPI(title="Flood Agent", lifespan=lifespan)

    @app.exception_handler(TableExpired)
    def expired_handler(request: Request, exc: TableExpired):
        # Any endpoint reading Result Table rows (rows, PSV, chart rows) gets a clear 410.
        return JSONResponse({"detail": "This result has expired and been deleted. Ask again to regenerate it."}, 410)

    @app.exception_handler(RefreshInProgress)
    def refresh_in_progress_handler(request: Request, exc: RefreshInProgress):
        return JSONResponse({"detail": str(exc)}, 409)

    @app.exception_handler(NotRefreshable)
    @app.exception_handler(RefreshFailed)
    def refresh_failed_handler(request: Request, exc: NotRefreshable | RefreshFailed):
        return JSONResponse({"detail": str(exc)}, 422)

    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
    pending: dict[str, tuple[Conversation, str, int, str, str | None]] = {}

    def sidebar(current: str | None, current_notebook: str | None = None) -> dict:
        return {
            "conversations": agent.store.list_conversations(), "current_id": current,
            "notebooks": agent.notebooks.list_notebooks(), "current_notebook_id": current_notebook,
            "picker": picker or [], "dev": dev,
        }

    models = [{"id": m.id, "label": m.label, "disabled": not has_api_key, "efforts": m.efforts,
               "default_effort": m.default_effort} for m in agent.config.models]
    models.append({"id": NO_MODEL, "label": "No model", "disabled": False, "efforts": [], "default_effort": None})
    default_model = agent.config.model if has_api_key else NO_MODEL

    def usage(tokens: int | None, model: str | None = None) -> dict:
        """The Context Usage for `model` (the default when None); No model has no window."""
        return context_usage(tokens, agent.config.window_of(model or default_model))

    def last_real_tokens(traces: list[dict]) -> int | None:
        """The latest real LLM call's prompt tokens: fake (No model) turns do not count."""
        real = [t for t in traces if t["model"] != NO_MODEL]
        return context_tokens(real[-1]) if real else None

    def latest_tokens(conversation_id: str | None) -> int | None:
        if not conversation_id or not agent.store.exists(conversation_id):
            return None
        return last_real_tokens([i["trace"] for i in agent.store.transcript(conversation_id) if i["type"] == "trace"])

    def known_model(choice: str) -> str:
        """`choice` if it is in the curated list or NO_MODEL (422 otherwise)."""
        if choice not in {m["id"] for m in models}:
            raise HTTPException(422, f"unknown model {choice!r}")
        return choice

    def model_choice(raw: str | None) -> str:
        """The model a prompt asked for, checked against the curated list and the API key (422 otherwise)."""
        choice = known_model(raw or default_model)
        if choice != NO_MODEL and not has_api_key:
            raise HTTPException(422, f"{choice} {NEEDS_KEY}: set it in .env and restart the app")
        return choice

    def effort_choice(model: str, raw: str | None) -> str | None:
        """The Effort a prompt asked for (None: the model's default), checked against the model's levels (422 otherwise)."""
        if model == NO_MODEL or not raw:
            return None
        spec = agent.config.spec_of(model)
        assert spec is not None  # model_choice has already vetted it
        if raw not in spec.efforts:
            raise HTTPException(422, f"{spec.label} does not offer reasoning effort {raw!r}")
        return raw

    def display_item(item: dict, expired: set[str]) -> dict:
        """A saved Display with what its template needs: the URLs it loads through, a chart's resolved
        spec, and whether its table has expired."""
        if item["kind"] == COUNT_KIND:
            return item
        extra = {**_table_display_urls(item["result_table"]), "expired": item["result_table"] in expired}
        if item["kind"] == "chart":
            extra["spec_json"] = json.dumps(resolve_chart_data(item["spec"], item["result_table"]))
        return {**item, **extra}

    def history(conversation_id: str) -> list[dict]:
        items = []
        transcript = agent.store.transcript(conversation_id)
        shown = {i["result_table"] for i in transcript if i["type"] == "display" and i["result_table"]}
        expired = shown - agent.tables.live_among(shown)
        prompt_at = None  # where the current turn's prompt is in `items`
        for item in transcript:
            if item["type"] == "display":
                items.append(display_item(item, expired))
            elif item["type"] == "trace":
                # The Trace goes right after the prompt, where the live view of the turn was; the note about
                # unseen values belongs to the reply, so it stays after it.
                flags = [e for e in item["trace"]["entries"] if e["kind"] == "flag"]
                if flags:
                    items.append({"type": "flags", "flags": flags})
                items.insert(prompt_at + 1 if prompt_at is not None else len(items), item)
            else:
                m = item["message"]
                if m["role"] in ("user", "assistant") and m.get("content"):
                    items.append({"type": m["role"], "text": m["content"], "turn_id": item["turn_id"]})
                    if m["role"] == "user":
                        prompt_at = len(items) - 1
        return items

    def model_picker() -> dict:
        return {"models": models, "default_model": default_model, "needs_key": NEEDS_KEY, "no_model": NO_MODEL}

    @app.get("/context-meter", response_class=HTMLResponse)
    def context_meter(model: str, conversation_id: str = ""):
        """The meter for the model selected for the next prompt (the Conversation's tokens, that model's window)."""
        known_model(model)
        return TEMPLATES.get_template("context_meter.html").render(context=usage(latest_tokens(conversation_id), model))

    @app.get("/", response_class=HTMLResponse)
    def index(request: Request):
        return TEMPLATES.TemplateResponse(request, "index.html", {**sidebar(None), "context": usage(None), **model_picker(), "items": []})

    @app.get("/c/{conversation_id}", response_class=HTMLResponse)
    def open_conversation(request: Request, conversation_id: str):
        if not agent.store.exists(conversation_id):
            raise HTTPException(404, "unknown conversation")
        items = history(conversation_id)
        traces = [i["trace"] for i in items if i["type"] == "trace"]
        return TEMPLATES.TemplateResponse(
            request, "index.html",
            {**sidebar(conversation_id), "context": usage(last_real_tokens(traces)),
             **model_picker(), "items": items, "current_id": conversation_id},
        )

    @app.get("/c/{conversation_id}/delete-preview")
    def delete_preview(conversation_id: str, turn_id: int | None = None):
        if not agent.store.exists(conversation_id):
            raise HTTPException(404, "unknown conversation")
        try:
            return agent.delete_preview(conversation_id, turn_id)
        except KeyError:
            raise HTTPException(404, "unknown turn")

    @app.post("/c/{conversation_id}/delete")
    def delete_conversation(conversation_id: str, tables: TablesChoice = Form("keep")):
        try:
            agent.delete_conversation(conversation_id, tables)
        except KeyError:
            raise HTTPException(404, "unknown conversation")
        return RedirectResponse("/", status_code=303)

    @app.post("/c/{conversation_id}/turns/{turn_id}/delete")
    def delete_turn(conversation_id: str, turn_id: int, tables: TablesChoice = Form("keep")):
        try:
            agent.delete_turn(conversation_id, turn_id, tables)
        except KeyError:
            raise HTTPException(404, "unknown turn")
        return RedirectResponse(f"/c/{conversation_id}", status_code=303)

    @app.post("/c/{conversation_id}/rename")
    def rename_conversation(conversation_id: str, title: str = Form("")):
        if not agent.store.exists(conversation_id):
            raise HTTPException(404, "unknown conversation")
        title = title.strip()[:60]
        if not title:
            raise HTTPException(422, "a conversation name cannot be blank")
        agent.store.rename_conversation(conversation_id, title)
        return RedirectResponse(f"/c/{conversation_id}", status_code=303)

    # --- Notebooks -------------------------------------------------------------------------------------

    def settings_label(model: str, effort: str | None, max_rows: int) -> str:
        """The settings chip's text: "Claude Sonnet 5.5 · high · ≤ 1,000 rows"."""
        label = next((m["label"] for m in models if m["id"] == model), model)
        return f"{label} · {effort or 'no effort'} · ≤ {max_rows:,} rows"

    def new_cell_settings() -> dict:
        """What a new AI Cell starts with: the settings last run (if that model is still usable), else the defaults."""
        last = agent.notebooks.last_settings()
        if last is None or not any(m["id"] == last["model"] and not m["disabled"] for m in models):
            last = {"model": default_model, "effort": None, "max_rows": DEFAULT_MAX_ROWS}
        spec = agent.config.spec_of(last["model"])
        effort = last["effort"] if spec and last["effort"] in spec.efforts else (spec.default_effort if spec else None)
        return {"model": last["model"], "effort": effort, "max_rows": last["max_rows"]}

    def add_ai_cell(notebook_id: str, after: str | None = None, before: str | None = None) -> str:
        s = new_cell_settings()
        return agent.add_ai_cell(notebook_id, model=s["model"], effort=s["effort"], max_rows=s["max_rows"], after=after, before=before)

    def add_sql_cell(notebook_id: str, after: str | None = None, before: str | None = None) -> str:
        last = agent.notebooks.last_settings("sql")
        return agent.add_sql_cell(notebook_id, max_rows=last["max_rows"] if last else DEFAULT_MAX_ROWS, after=after, before=before)

    def output_items(run: dict, refreshable_counts: bool = True) -> list[dict]:
        """What a finished (or stopped) Run shows under its prompt: the answer's text and its Displays, in order.
        An AI Cell's Count Displays refresh; a SQL Cell's do not (its Run is its refresh)."""
        transcript = agent.notebooks.transcript(run["id"])
        shown = {i["result_table"] for i in transcript if i["type"] == "display" and i["result_table"]}
        expired = shown - agent.tables.live_among(shown)
        items: list[dict] = []
        for item in transcript:
            if item["type"] == "display":
                if item["kind"] == COUNT_KIND:  # its id is a notebook row: it refreshes through the notebook's endpoint
                    item = {**item, "display_id": item["display_id"] if refreshable_counts else None,
                            "refresh_url": NOTEBOOK_COUNT_REFRESH.format(item["display_id"])}
                items.append(display_item(item, expired))
                continue
            m = item["message"]
            if m["role"] == "assistant" and m.get("content"):
                text = m["content"].removesuffix(STOPPED_NOTE).rstrip()  # the cell says it in its own words
                if text:
                    items.append({"type": "assistant", "text": text})
        return items

    def refreshed_elsewhere(run: dict) -> int | None:
        """When a SQL Cell's table was last refreshed (in the Tables dialog, say) after the Run finished, else None."""
        name = (run["result"] or {}).get("table")
        if not name or run["finished_at"] is None:
            return None
        try:
            as_of = agent.tables[name].as_of_ms
        except (KeyError, TableExpired):  # dropped or expired
            return None
        return as_of if as_of > run["finished_at"] else None

    def cell_view(cell: dict, starting: bool = False, stale: bool = False) -> dict:
        """What the cell template needs. `starting`: its run has been asked for and its stream not yet opened,
        so it shows as running with no output."""
        draft, run = cell["draft"], cell["run"]
        if cell["kind"] == MARKDOWN:
            return {"id": cell["id"], "notebook_id": cell["notebook_id"], "kind": MARKDOWN,
                    "text": draft.get("text", ""), "send_to_ai": bool(draft.get("send_to_ai"))}
        model = draft.get("model") or default_model
        view = {
            "id": cell["id"], "notebook_id": cell["notebook_id"], "kind": cell["kind"], "prompt": draft.get("prompt", ""),
            "sql": draft.get("sql", ""), "command": draft.get("command") or "", "table": draft.get("table") or "",
            "model": model, "effort": draft.get("effort") or "", "max_rows": draft.get("max_rows") or DEFAULT_MAX_ROWS,
            "running": starting, "streaming": starting, "polling": False, "status": None, "number": None,
            "output": [], "footer": None, "ran": None, "edited": False, "earlier_tables": [],
            "stale": stale,
        }
        view["chip"] = settings_label(model, view["effort"], view["max_rows"])
        if starting or run is None:
            return view
        view["number"], view["status"] = run["number"], run["status"]
        if run["status"] == "running" and agent.running_cell(cell["notebook_id"]) == cell["id"]:
            view["running"] = view["polling"] = True  # opened mid-run: nothing streams to this page, so it re-fetches
        view["output"] = output_items(run, refreshable_counts=cell["kind"] != "sql")
        if cell["kind"] == "sql":
            view["result"] = run["result"]
            view["ran_utc"] = time.strftime("%H:%M UTC", time.gmtime(run["started_at"] / 1000))
            view["started_at_ms"] = run["started_at"]
            view["refreshed_at_ms"] = refreshed_elsewhere(run)
            return view
        ran = run["settings"]
        view["ran"] = {"prompt": run["prompt"], "model": ran["model"], "effort": ran["effort"] or "",
                       "max_rows": ran["max_rows"]}
        view["edited"] = (draft["prompt"], model, view["effort"], view["max_rows"]) != tuple(view["ran"].values())
        if not view["running"]:
            view["earlier_tables"] = [
                {"name": t.name, "settings_url": _table_display_urls(t.name)["settings_url"]}
                for t in agent.tables.earlier_tables_of_cell(cell["id"], run["id"])]
        trace = run["trace"]
        view["footer"] = {
            "started_at_ms": run["started_at"], "ran_utc": time.strftime("%H:%M UTC", time.gmtime(run["started_at"] / 1000)),
            "settings": settings_label(ran["model"], ran["effort"], ran["max_rows"]),
            "stopped": run["status"] == "stopped", "failed": run["status"] == "failed", "trace": trace,
            "calls": trace["totals"]["llm_steps"] if trace else None,
            "cost": trace["totals"]["cost"] if trace else None,
            "context": usage(run["context_tokens"], ran["model"]),
        }
        return view

    def cell_html(notebook_id: str, cell_id: str, template: str, starting: bool = False) -> str:
        cell = agent.notebooks.cell(cell_id)
        stale = cell["id"] in agent.stale_cells(notebook_id)
        return TEMPLATES.get_template(template).render(
            cell=cell_view(cell, starting, stale), notebook_id=notebook_id, **model_picker())

    def known_cell(notebook_id: str, cell_id: str) -> dict:
        if not agent.notebooks.exists(notebook_id):
            raise HTTPException(404, "unknown notebook")
        try:
            cell = agent.notebooks.cell(cell_id)
        except KeyError:
            raise HTTPException(404, "unknown cell")
        if cell["notebook_id"] != notebook_id:
            raise HTTPException(404, "unknown cell")
        return cell

    def posted_settings(prompt: str, model: str, effort: str, max_rows: str) -> dict:
        chosen = model_choice(model)
        return {"prompt": prompt, "model": chosen, "effort": effort_choice(chosen, effort),
                "max_rows": _parse_max_rows(max_rows)}

    def posted_command(command: str, table: str) -> dict:
        if command and command not in SHOW_COMMANDS:
            raise HTTPException(422, f"unknown command {command!r}")
        return {"command": command, "table": table.strip()}

    @app.post("/notebooks")
    def create_notebook():
        """The sidebar's "+": a new "Notebook N" with one empty AI Cell, opened."""
        notebook_id = agent.create_notebook()
        add_ai_cell(notebook_id)
        return RedirectResponse(f"/n/{notebook_id}", status_code=303)

    @app.get("/api/notebooks")
    def search_notebooks(q: str = ""):
        """The notebooks (id, title) whose title or cell text contains `q`, most recent activity first."""
        return [{"id": n["id"], "title": n["title"]} for n in agent.notebooks.list_notebooks(q)]

    @app.post("/n/{notebook_id}/rename")
    def rename_notebook(notebook_id: str, title: str = Form("")):
        try:
            agent.rename_notebook(notebook_id, title)
        except KeyError:
            raise HTTPException(404, "unknown notebook")
        except ValueError as e:
            raise HTTPException(422, str(e))
        return RedirectResponse(f"/n/{notebook_id}", status_code=303)

    @app.get("/n/{notebook_id}/delete-preview")
    def notebook_delete_preview(notebook_id: str):
        try:
            return agent.notebook_delete_preview(notebook_id)
        except KeyError:
            raise HTTPException(404, "unknown notebook")
        except NotebookBusy as e:
            raise HTTPException(409, str(e))

    @app.post("/n/{notebook_id}/delete")
    def delete_notebook(notebook_id: str, tables: TablesChoice = Form("keep")):
        try:
            agent.delete_notebook(notebook_id, tables)
        except KeyError:
            raise HTTPException(404, "unknown notebook")
        except NotebookBusy as e:
            raise HTTPException(409, str(e))
        return RedirectResponse("/", status_code=303)

    @app.get("/n/{notebook_id}", response_class=HTMLResponse)
    def open_notebook(request: Request, notebook_id: str):
        if not agent.notebooks.exists(notebook_id):
            raise HTTPException(404, "unknown notebook")
        stale = agent.stale_cells(notebook_id)
        cells = [cell_view(c, stale=c["id"] in stale) for c in agent.notebooks.cells(notebook_id)]
        return TEMPLATES.TemplateResponse(request, "notebook.html", {
            **sidebar(None, notebook_id), **model_picker(), "notebook_id": notebook_id,
            "title": agent.notebooks.title_of(notebook_id), "cells": cells, "stale_ids": sorted(stale), "notebook_page": True,
        })

    @app.get("/n/{notebook_id}/stale")
    def stale_cells(notebook_id: str) -> list[str]:
        """The ids of the notebook's stale AI Cells: the page re-reads them after anything that can change one."""
        if not agent.notebooks.exists(notebook_id):
            raise HTTPException(404, "unknown notebook")
        stale = agent.stale_cells(notebook_id)
        return [c["id"] for c in agent.notebooks.cells(notebook_id) if c["id"] in stale]

    @app.post("/n/{notebook_id}/cells", response_class=HTMLResponse)
    def add_cell(notebook_id: str, kind: str = Form("ai"), after: str | None = Form(None), at: str = Form("")):
        """Insert a cell after `after` (or at the end; at the very top with `at=start`); returns it, with its
        "add a cell" bar, for htmx to place."""
        if not agent.notebooks.exists(notebook_id):
            raise HTTPException(404, "unknown notebook")
        if kind not in ("ai", "sql", MARKDOWN):
            raise HTTPException(422, f"unknown cell kind {kind!r}")
        if after:
            known_cell(notebook_id, after)
        before = None
        if at == "start" and not after:
            first = agent.notebooks.cells(notebook_id)
            before = first[0]["id"] if first else None
        if kind == MARKDOWN:
            cell_id = agent.add_markdown_cell(notebook_id, after=after or None, before=before)
        elif kind == "sql":
            cell_id = add_sql_cell(notebook_id, after or None, before)
        else:
            cell_id = add_ai_cell(notebook_id, after or None, before)
        return cell_html(notebook_id, cell_id, "notebook_slot.html")

    def cell_operation(notebook_id: str, cell_id: str, operation):
        """Run a cell operation; a running cell cannot be moved, copied or deleted (409)."""
        known_cell(notebook_id, cell_id)
        try:
            return operation()
        except NotebookBusy as e:
            raise HTTPException(409, str(e))

    @app.post("/n/{notebook_id}/cells/{cell_id}/move", status_code=204)
    def move_cell(notebook_id: str, cell_id: str, direction: Literal["up", "down"] = Form(...)):
        cell_operation(notebook_id, cell_id, lambda: agent.move_cell(notebook_id, cell_id, direction))

    @app.post("/n/{notebook_id}/cells/{cell_id}/copy", response_class=HTMLResponse)
    def copy_cell(notebook_id: str, cell_id: str):
        """Copy the cell's Draft into a new cell just below it; returns it with its bar, for htmx to place."""
        new_id = cell_operation(notebook_id, cell_id, lambda: agent.copy_cell(notebook_id, cell_id))
        return cell_html(notebook_id, new_id, "notebook_slot.html")

    @app.get("/n/{notebook_id}/cells/{cell_id}/delete-preview")
    def cell_delete_preview(notebook_id: str, cell_id: str):
        return cell_operation(notebook_id, cell_id, lambda: agent.cell_delete_preview(notebook_id, cell_id))

    @app.post("/n/{notebook_id}/cells/{cell_id}/delete", status_code=204)
    def delete_cell(notebook_id: str, cell_id: str, tables: TablesChoice = Form("keep")):
        cell_operation(notebook_id, cell_id, lambda: agent.delete_cell(notebook_id, cell_id, tables))

    @app.get("/n/{notebook_id}/cells/{cell_id}", response_class=HTMLResponse)
    def get_cell(notebook_id: str, cell_id: str):
        known_cell(notebook_id, cell_id)
        return cell_html(notebook_id, cell_id, "notebook_cell.html")

    @app.post("/n/{notebook_id}/cells/{cell_id}/draft", status_code=204)
    def save_draft(notebook_id: str, cell_id: str, prompt: str = Form(""), model: str = Form(""),
                   effort: str = Form(""), max_rows: str = Form(""), sql: str = Form(""),
                   command: str = Form(""), table: str = Form("")):
        """Remember a cell's edited prompt (or SQL) and settings without running them."""
        cell = known_cell(notebook_id, cell_id)
        if cell["kind"] == "sql":
            agent.notebooks.edit_cell(cell_id, sql=sql, max_rows=_parse_max_rows(max_rows), **posted_command(command, table))
            return
        agent.notebooks.edit_ai_cell(cell_id, **posted_settings(prompt, model, effort, max_rows))

    @app.post("/n/{notebook_id}/cells/{cell_id}/markdown", status_code=204)
    def save_markdown(notebook_id: str, cell_id: str, text: str = Form(""), send_to_ai: str = Form("")):
        """Remember a Markdown Cell's text and its Send to AI tick (a missing tick is unticked)."""
        if known_cell(notebook_id, cell_id)["kind"] != MARKDOWN:
            raise HTTPException(422, "not a Markdown Cell")
        agent.notebooks.edit_ai_cell(cell_id, text=text, send_to_ai=bool(send_to_ai))

    @app.post("/n/{notebook_id}/cells/{cell_id}/earlier-tables/delete", response_class=HTMLResponse)
    def delete_earlier_tables(notebook_id: str, cell_id: str):
        """Drop the `rt_` tables of the cell's earlier Runs (never Saved Tables); returns the cell."""
        cell = known_cell(notebook_id, cell_id)
        if cell["run"]:
            agent.tables.drop_earlier_of_cell(cell_id, cell["run"]["id"])
        return cell_html(notebook_id, cell_id, "notebook_cell.html")

    @app.post("/n/{notebook_id}/cells/{cell_id}/run", response_class=HTMLResponse)
    def run_cell(notebook_id: str, cell_id: str, prompt: str = Form(""), model: str = Form(""),
                 effort: str = Form(""), max_rows: str = Form(""), sql: str = Form(""),
                 command: str = Form(""), table: str = Form("")):
        """Save the cell's prompt (or SQL) and settings as its Draft and return the cell, running; its stream starts the Run."""
        cell = known_cell(notebook_id, cell_id)
        if cell["kind"] not in ("ai", "sql"):
            raise HTTPException(422, "only AI and SQL Cells run")
        is_sql = cell["kind"] == "sql"
        if is_sql and command:
            if command == DESCRIBE_TABLE and not table.strip():
                raise HTTPException(422, "Pick a table to describe")
        elif is_sql and is_show_statement(sql):
            raise HTTPException(422, SHOW_IN_TEXTBOX)
        elif not (sql if is_sql else prompt).strip():
            raise HTTPException(422, "Write some SQL to run" if is_sql else "Write a prompt to run")
        if agent.running_cell(notebook_id):
            raise HTTPException(409, "A cell in this notebook is already running")
        if is_sql:
            agent.notebooks.edit_cell(cell_id, sql=sql, max_rows=_parse_max_rows(max_rows), **posted_command(command, table))
        else:
            agent.notebooks.edit_ai_cell(cell_id, **posted_settings(prompt, model, effort, max_rows))
        return cell_html(notebook_id, cell_id, "notebook_cell.html", starting=True)

    @app.get("/n/{notebook_id}/cells/{cell_id}/stream")
    async def stream_cell(notebook_id: str, cell_id: str):
        known_cell(notebook_id, cell_id)

        async def events():
            live = LiveCounter()
            try:
                async with aclosing(cast(AsyncGenerator, agent.run_cell(notebook_id, cell_id))) as run:
                    async for ev in run:
                        sse = live_event_sse(ev, cell_id, live, notebook=True)  # a SQL Cell's event has no display_id
                        if sse is not None:
                            yield sse
                        elif isinstance(ev, TurnEnd):
                            break
            except NotebookBusy:
                yield _sse("text", '<span class="md-delta">A cell in this notebook is already running.</span>')
            except Exception as e:  # keep the stream well-formed for the browser
                yield _sse("text", f'<span class="md-delta">{escape(f" [error: {type(e).__name__}]")}</span>')
            yield _sse("end", "done")

        return StreamingResponse(events(), media_type="text/event-stream")

    @app.post("/n/{notebook_id}/cells/{cell_id}/stop", status_code=204)
    async def stop_cell(notebook_id: str, cell_id: str):  # async: it touches the event loop's tasks
        """Stop a running cell now; its stream still ends. Does nothing if it already ended."""
        known_cell(notebook_id, cell_id)
        agent.stop_cell(cell_id)

    if dev:
        @app.post("/api/dev/shift-timestamps")
        def shift_timestamps():
            """Move the Telemetry Database's timestamps so its latest record is the current time."""
            delta = dev_tools.shift_to_now(agent.config.telemetry_db, agent.semantic_layer, agent.config.clock())
            return {"delta_ms": delta}

    @app.post("/api/lookup-tables")
    def add_lookup_table(name: str = Form(""), rows: str = Form(""), overwrite: str = Form("")):
        """Create a Lookup Table from pasted rows. Plain HTTP, never an MCP tool."""
        try:
            return agent.tables.add_lookup(name, rows, overwrite=bool(overwrite))
        except InvalidLookupTable as e:
            raise HTTPException(422, str(e))

    @app.post("/api/lookup-tables/reformat")
    def reformat_lookup_rows(rows: str = Form(""), sep: str = Form("comma")):
        """Edited rows re-shown in another separator, for the edit dialog. 422 if they do not parse."""
        try:
            return {"text": lookup_tables.format_rows(lookup_tables.parse_rows(rows), sep)}
        except InvalidLookupTable as e:
            raise HTTPException(422, str(e))

    @app.get("/api/lookup-tables/{name}")
    def lookup_table_text(name: str, sep: str = "comma"):
        """A Lookup Table's rows as text, for the edit dialog. Plain HTTP, never an MCP tool."""
        try:
            return agent.tables.lookup_text(name, sep)
        except InvalidLookupTable as e:
            raise HTTPException(422, str(e))
        except KeyError:
            raise HTTPException(404, "unknown lookup table")

    @app.put("/api/lookup-tables/{name}")
    def update_lookup_table(name: str, rows: str = Form("")):
        """Save edited rows over a Lookup Table. Plain HTTP, never an MCP tool."""
        try:
            return agent.tables.replace_lookup(name, rows)
        except InvalidLookupTable as e:
            raise HTTPException(422, str(e))
        except KeyError:
            raise HTTPException(404, "unknown lookup table")

    @app.delete("/api/tables/{name}", status_code=204)
    def delete_table(name: str):
        """Delete a Lookup, Saved or Result Table from the Tables dialog. Plain HTTP, never an MCP tool."""
        try:
            agent.tables.delete(name)
        except ValueError as e:
            raise HTTPException(400, str(e))
        except KeyError:
            raise HTTPException(404, "unknown table")

    @app.get("/api/tables/{name}/display", response_class=HTMLResponse)
    def table_display(name: str):
        """A Saved or Result Table as the chat's table Display, for the Tables dialog's viewer.
        404 for any other table (Telemetry, Lookup, unknown); 410 once it has expired."""
        try:
            agent.tables[name]
        except KeyError:
            raise HTTPException(404, "not a Saved or Result Table")
        return TEMPLATES.get_template("display_table.html").render(**_table_display_urls(name), result_table=name)

    @app.get("/api/tables")
    def list_tables():
        """Names of the tables the user can query, for the Tables dialog. Plain HTTP, never an MCP tool."""
        return agent.tables.listing()

    @app.get("/api/tables/{name}/columns")
    def table_columns(name: str):
        """A readable table's column names and types, for a SQL Cell's click-to-insert list. Never rows, never
        an MCP tool. 404 for a table the user may not query."""
        kind = agent.tables.kind_of(name)
        if kind is None:
            raise HTTPException(404, "unknown table")
        columns = queries.table_columns(agent.config.telemetry_db, agent.config.results_db, agent.tables.canonical(name), kind == "telemetry")
        if columns is None:
            raise HTTPException(404, "unknown table")
        return {"name": agent.tables.canonical(name), "kind": kind, "columns": columns}

    @app.post("/chat", response_class=HTMLResponse)
    def chat(request: Request, message: str = Form(...), conversation_id: str = Form(""), max_rows: str = Form(...),
             model: str | None = Form(None), effort: str | None = Form(None)):
        max_result_rows = _parse_max_rows(max_rows)
        chosen_model = model_choice(model)
        chosen_effort = effort_choice(chosen_model, effort)
        if conversation_id:
            try:
                conversation = agent.open_conversation(conversation_id)
            except KeyError:
                raise HTTPException(404, "unknown conversation")
        else:
            conversation = Conversation()
        turn_id = uuid.uuid4().hex
        pending[turn_id] = (conversation, message, max_result_rows, chosen_model, chosen_effort)
        if conversation.id is None:  # persist now so the sidebar and form can reference it
            conversation.id = agent.create_conversation()
        return TEMPLATES.TemplateResponse(
            request, "turn.html",
            {"turn_id": turn_id, "message": message, **sidebar(conversation.id)},
        )

    @app.get("/chat/stream/{turn_id}")
    async def stream(request: Request, turn_id: str):
        entry = pending.pop(turn_id, None)
        if entry is None:
            raise HTTPException(404)
        conversation, message, max_result_rows, chosen_model, chosen_effort = entry

        async def events():
            live = LiveCounter()
            try:
                async for ev in agent.run_turn(conversation, message, max_result_rows, chosen_model, chosen_effort, turn_key=turn_id):
                    sse = live_event_sse(ev, turn_id, live)
                    if sse is not None:
                        yield sse
                    if isinstance(ev, TraceReady):
                        if ev.turn_id is not None and conversation.id:
                            yield _sse("turn", TEMPLATES.get_template("turn_delete.html").render(
                                conversation_id=conversation.id, turn_id=ev.turn_id))
                        trace_html = TEMPLATES.get_template("trace.html").render(trace=ev.trace)
                        yield _sse("trace", trace_html)
                    elif isinstance(ev, TurnEnd):
                        break
            except Exception as e:  # keep the stream well-formed for the browser
                yield _sse("text", f'<span class="md-delta">{escape(f" [error: {type(e).__name__}]")}</span>')
            yield _sse("end", "done")

        return StreamingResponse(events(), media_type="text/event-stream")

    @app.post("/chat/stop/{turn_id}", status_code=204)
    async def stop(turn_id: str):  # async: it touches the event loop's tasks
        """Stop a running turn now; its stream still ends with the Trace. Does nothing if it already ended."""
        agent.stop_turn(turn_id)

    @app.get("/api/results/{result_table}/rows")
    def rows(
        result_table: str,
        page: int = Query(1, ge=1),
        page_size: int = Query(20, ge=1, le=500),
        sort: str | None = None,
        dir: Literal["asc", "desc"] = "asc",
    ):
        try:
            return agent.fetch_rows(
                result_table, page=page, page_size=page_size, sort=sort, descending=dir == "desc"
            )
        except KeyError:
            raise HTTPException(404, "unknown result table")
        except InvalidSort:
            raise HTTPException(400, "unknown sort column")

    @app.get("/api/results/{result_table}/settings")
    def get_result_table_settings(result_table: str):
        """A live Result Table's name, Retention in days and time left in ms (for a Display's name header)."""
        try:
            return agent.tables[result_table].to_json()
        except KeyError:
            raise HTTPException(404, "unknown result table")

    @app.post("/api/results/{result_table}/refresh")
    def refresh_result_table(result_table: str):
        """Refresh a Result or Saved Table: run its stored query again and replace it under the same name.
        Plain HTTP, never an MCP tool; the LLM is not involved. 404 unknown, 410 expired, 409 already
        refreshing, 422 not refreshable or the query failed (the table is then unchanged). Returns the
        refreshed table's metadata."""
        try:
            table = agent.refresh_table(result_table)
        except KeyError:
            raise HTTPException(404, "unknown result table")
        info = queries.describe_results_table(agent.config.results_db, table.name) or {}
        return {**table.to_json(), **info, "truncated_to": table.truncated_to}

    @app.post("/api/explain")
    def explain(sql: str = Form("")):
        """The Query Plan of a Generated SQL from a Trace, for the user only: no LLM, nothing stored.
        It may read any live table. 422 with the plain reason (rejected SQL, a database error)."""
        try:
            plan = queries.explain_query(agent.config.telemetry_db, agent.config.results_db, sql,
                                         agent.config.query_timeout_s, agent.tables.readable())
        except (queries.QueryRejected, sqlite3.Error) as e:
            raise HTTPException(422, str(e))
        return {"plan": plan}

    @app.post("/api/displays/{display_id}/refresh")
    def refresh_count_display(display_id: int):
        """Refresh a Count Display: run its stored count query again and overwrite the number and "as of"
        time. Plain HTTP, never an MCP tool; the LLM is not involved. 404 unknown, 409 already refreshing,
        422 the query failed (the old number is then kept). Returns {value, computed_at_ms}."""
        try:
            return agent.refresh_count(display_id)
        except KeyError:
            raise HTTPException(404, "unknown Count Display")

    @app.post("/api/notebook-displays/{display_id}/refresh")
    def refresh_notebook_count_display(display_id: int):
        """Refresh an AI Cell's Count Display, as `refresh_count_display` does. Never a Run: the cell's Run
        and what cells below are sent are untouched."""
        try:
            return agent.refresh_count(display_id, in_notebook=True)
        except KeyError:
            raise HTTPException(404, "unknown Count Display")

    @app.post("/api/results/{result_table}/settings")
    def result_table_settings(
        result_table: str, name: str | None = Form(None), retention_days: str | None = Form(None)
    ):
        """Rename a Result Table and/or set how long it is kept (days from its creation)."""
        if name is not None:
            name = name.strip()
        days = None if retention_days is None else _parse_retention_days(retention_days)
        try:
            return agent.tables.update(result_table, name, days).to_json()
        except KeyError:
            raise HTTPException(404, "unknown result table")
        except ValueError as e:
            raise HTTPException(422, str(e))

    @app.get("/api/results/{result_table}/psv")
    def psv_download(result_table: str):
        try:
            columns, rows_iter = agent.iter_all_rows(result_table)
        except KeyError:
            raise HTTPException(404, "unknown result table")

        def body():
            buf = io.StringIO()
            w = csv.writer(buf, delimiter="|", lineterminator="\r\n")
            w.writerow([_psv_safe(c) for c in columns])
            for row in rows_iter:
                w.writerow([_psv_safe(v) for v in row])
                if buf.tell() > 65536:
                    yield buf.getvalue()
                    buf.seek(0)
                    buf.truncate()
            yield buf.getvalue()

        return StreamingResponse(
            body(),
            media_type="text/plain; charset=utf-8",
            headers={"Content-Disposition": f'attachment; filename="{result_table}.psv"'},
        )

    @app.get("/api/results/{result_table}/chart-rows")
    def chart_rows(result_table: str):
        """Internal data path for charts (row objects, chart cap). Never an MCP tool."""
        try:
            return agent.fetch_chart_rows(result_table)
        except KeyError:
            raise HTTPException(404, "unknown result table")

    return app


def _parse_max_rows(raw: str) -> int:
    """The "Max result rows" choice (the only source of the row cap): a positive integer."""
    raw = raw.strip().replace(",", "")
    if not raw.isascii() or not raw.isdigit() or int(raw) < 1:
        raise HTTPException(422, "Max result rows must be a whole number of at least 1")
    return int(raw)


def _parse_retention_days(raw: str) -> float:
    """The posted number of days; the Catalog's Retention policy decides whether it is in range."""
    try:
        return float(raw)
    except ValueError:
        raise HTTPException(422, "retention must be a number of days")


def _psv_safe(v):
    """Neutralise spreadsheet formula injection in text cells; numbers pass through."""
    if v is None:
        return ""
    if isinstance(v, str) and v[:1] in ("=", "+", "-", "@", "\t", "\r"):
        return "'" + v
    return v
