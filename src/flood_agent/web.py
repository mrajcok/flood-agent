"""FastAPI adapter: server-rendered Jinja + htmx + Alpine, streaming over SSE.

A thin layer over `Agent.run_turn`. The row-JSON endpoint is the Agent's internal data
path to the browser; it is a plain HTTP route, never registered as an MCP tool.
"""

import csv
import io
import json
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from html import escape
from pathlib import Path
from typing import Literal

from fastapi import FastAPI, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from flood_agent import lookup_tables
from flood_agent.agent import (
    NO_MODEL,
    Agent,
    Conversation,
    DisplayEvent,
    FlagEvent,
    LlmCallStarted,
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
from flood_agent.queries import InvalidSort
from flood_agent.trace import context_tokens, short_model, split_system_prompt

TEMPLATES = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))
TEMPLATES.env.filters["split_system_prompt"] = split_system_prompt
TEMPLATES.env.filters["short_model"] = short_model
STATIC_DIR = Path(__file__).parent / "static"


def static_version(name: str) -> int:
    """Changes whenever the static file does, so a browser never reuses a stale cached copy."""
    return (STATIC_DIR / name).stat().st_mtime_ns


TEMPLATES.env.globals["static_version"] = static_version


def _sse(event: str, data: str) -> str:
    lines = "".join(f"data: {line}\n" for line in data.split("\n"))
    return f"event: {event}\n{lines}\n"


@dataclass(frozen=True)
class PickerEntry:
    """One clickable prompt in the Fake Mode picker, with the follow-ups listed under it."""

    prompt: str
    fails_on_purpose: bool = False
    follow_ups: list["PickerEntry"] = field(default_factory=list)


def _table_display_urls(result_table: str) -> dict:
    """The URLs a table Display (display_table.html) loads and saves through."""
    base = f"/api/results/{result_table}"
    return {"rows_url": f"{base}/rows", "psv_url": f"{base}/psv", "settings_url": f"{base}/settings"}


NEEDS_KEY = "needs OPENROUTER_API_KEY"


def create_app(agent: Agent, picker: list[PickerEntry] | None = None, has_api_key: bool = True) -> FastAPI:
    """The Model dropdown lists the curated models, disabled when `has_api_key` is False, then "No model"
    (Fake Mode, always selectable). The `picker` Canned Prompts show on the page only while it is selected."""
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

    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
    pending: dict[str, tuple[Conversation, str, int, str, str | None]] = {}

    def sidebar(current: str | None) -> dict:
        return {
            "conversations": agent.store.list_conversations(), "current_id": current,
            "picker": picker or [],
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

    def history(conversation_id: str) -> list[dict]:
        items = []
        transcript = agent.store.transcript(conversation_id)
        shown = {i["result_table"] for i in transcript if i["type"] == "display"}
        expired = shown - agent.tables.live_among(shown)
        prompt_at = None  # where the current turn's prompt is in `items`
        for item in transcript:
            if item["type"] == "display" and item["kind"] == "chart":
                spec = resolve_chart_data(item["spec"], item["result_table"])
                items.append({
                    **item, **_table_display_urls(item["result_table"]), "spec_json": json.dumps(spec),
                    "expired": item["result_table"] in expired,
                })
            elif item["type"] == "display":
                items.append(
                    {
                        **item,
                        **_table_display_urls(item["result_table"]),
                        "expired": item["result_table"] in expired,
                    }
                )
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
            call_number = 0
            try:
                async for ev in agent.run_turn(conversation, message, max_result_rows, chosen_model, chosen_effort, turn_key=turn_id):
                    if isinstance(ev, TextDelta):
                        yield _sse("text", f'<span class="md-delta">{escape(ev.text)}</span>')  # markdown.js renders it
                    elif isinstance(ev, DisplayEvent) and ev.kind == "chart":
                        html = TEMPLATES.get_template("display_chart.html").render(
                            spec_json=json.dumps(ev.spec), **_table_display_urls(ev.result_table)
                        ).strip()  # no stray newlines: the reply box keeps whitespace
                        yield _sse("display", html)
                    elif isinstance(ev, DisplayEvent):
                        html = TEMPLATES.get_template("display_table.html").render(
                            **_table_display_urls(ev.result_table),
                            result_table=ev.result_table,
                            columns=ev.columns,
                        ).strip()
                        yield _sse("display", html)
                    elif isinstance(ev, LlmCallStarted):
                        call_number = ev.number
                        yield _sse("live-step", TEMPLATES.get_template("live_step.html").render(number=ev.number, turn_id=turn_id).strip())
                    elif isinstance(ev, TraceLive):
                        entry_html = TEMPLATES.get_template("trace_entry.html").render(e=ev.entry, step=call_number).strip()
                        oob = f'<div hx-swap-oob="beforeend:#live-body-{turn_id}-{call_number}">{entry_html}</div>'
                        if ev.entry["kind"] == "llm_step":  # the call is over: its summary line gets the figures
                            meta = TEMPLATES.get_template("live_meta.html").render(e=ev.entry).strip()
                            oob += f'<span hx-swap-oob="innerHTML:#live-meta-{turn_id}-{call_number}">{meta}</span>'
                        yield _sse("entry", oob)
                    elif isinstance(ev, FlagEvent):
                        yield _sse("flag", TEMPLATES.get_template("flag_note.html").render(flags=ev.items))
                    elif isinstance(ev, TraceReady):
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

    @app.post("/api/results/{result_table}/settings")
    def result_table_settings(
        result_table: str, name: str | None = Form(None), retention_days: str | None = Form(None)
    ):
        """Rename a Result Table and/or set how long it is kept (days from its creation)."""
        if name is not None:
            name = name.strip()
        days = None if retention_days is None else _parse_retention_days(retention_days)
        try:
            return agent.update_result_table(result_table, name, days)
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


MAX_RETENTION_DAYS = 3650


def _parse_retention_days(raw: str) -> float:
    try:
        days = float(raw)
    except ValueError:
        days = 0.0
    if not 0 < days <= MAX_RETENTION_DAYS:  # also rejects nan
        raise HTTPException(422, f"retention must be more than 0 and at most {MAX_RETENTION_DAYS} days")
    return days


def _psv_safe(v):
    """Neutralise spreadsheet formula injection in text cells; numbers pass through."""
    if v is None:
        return ""
    if isinstance(v, str) and v[:1] in ("=", "+", "-", "@", "\t", "\r"):
        return "'" + v
    return v
