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
    Agent,
    Conversation,
    DisplayEvent,
    FlagEvent,
    ResultTableExpired,
    TextDelta,
    TraceReady,
    TurnEnd,
    resolve_chart_data,
)
from flood_agent.lookup_tables import InvalidLookupTable
from flood_agent.queries import InvalidSort
from flood_agent.store import TablesChoice
from flood_agent.trace import split_system_prompt

TEMPLATES = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))
TEMPLATES.env.filters["split_system_prompt"] = split_system_prompt
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


def create_app(agent: Agent, picker: list[PickerEntry] | None = None, fake_mode: bool = False) -> FastAPI:
    """In Fake Mode the page shows a badge and the `picker` list of prompts."""
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        # One long-lived MCP server for the app; a server that can't start fails app startup.
        async with agent:
            yield

    app = FastAPI(title="Flood Agent", lifespan=lifespan)

    @app.exception_handler(ResultTableExpired)
    def expired_handler(request: Request, exc: ResultTableExpired):
        # Any endpoint reading Result Table rows (rows, PSV, chart rows) gets a clear 410.
        return JSONResponse({"detail": "This result has expired and been deleted. Ask again to regenerate it."}, 410)

    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
    pending: dict[str, tuple[Conversation, str, int]] = {}

    def sidebar(current: str | None) -> dict:
        return {
            "conversations": agent.store.list_conversations(), "current_id": current,
            "fake_mode": fake_mode, "picker": picker or [],
        }

    def history(conversation_id: str) -> list[dict]:
        items = []
        transcript = agent.store.transcript(conversation_id)
        expired = agent.expired_among({i["result_table"] for i in transcript if i["type"] == "display"})
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
                items.append({**item, "flags": [e for e in item["trace"]["entries"] if e["kind"] == "flag"]})
            else:
                m = item["message"]
                if m["role"] in ("user", "assistant") and m.get("content"):
                    items.append({"type": m["role"], "text": m["content"], "turn_id": item["turn_id"]})
        return items

    @app.get("/", response_class=HTMLResponse)
    def index(request: Request):
        return TEMPLATES.TemplateResponse(request, "index.html", {**sidebar(None), "items": []})

    @app.get("/c/{conversation_id}", response_class=HTMLResponse)
    def open_conversation(request: Request, conversation_id: str):
        if not agent.store.exists(conversation_id):
            raise HTTPException(404, "unknown conversation")
        return TEMPLATES.TemplateResponse(
            request, "index.html",
            {**sidebar(conversation_id), "items": history(conversation_id), "current_id": conversation_id},
        )

    @app.get("/c/{conversation_id}/delete-preview")
    def delete_preview(conversation_id: str, turn_id: int | None = None):
        if not agent.store.exists(conversation_id):
            raise HTTPException(404, "unknown conversation")
        try:
            return agent.store.delete_preview(conversation_id, turn_id)
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
            return agent.add_lookup_table(name, rows, overwrite=bool(overwrite))
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
            return agent.lookup_table_text(name, sep)
        except InvalidLookupTable as e:
            raise HTTPException(422, str(e))
        except KeyError:
            raise HTTPException(404, "unknown lookup table")

    @app.put("/api/lookup-tables/{name}")
    def update_lookup_table(name: str, rows: str = Form("")):
        """Save edited rows over a Lookup Table. Plain HTTP, never an MCP tool."""
        try:
            return agent.update_lookup_table(name, rows)
        except InvalidLookupTable as e:
            raise HTTPException(422, str(e))
        except KeyError:
            raise HTTPException(404, "unknown lookup table")

    @app.delete("/api/tables/{name}", status_code=204)
    def delete_table(name: str):
        """Delete a Lookup, Saved or Result Table from the Tables dialog. Plain HTTP, never an MCP tool."""
        try:
            agent.delete_table(name)
        except ValueError as e:
            raise HTTPException(400, str(e))
        except KeyError:
            raise HTTPException(404, "unknown table")

    @app.get("/api/tables/{name}/display", response_class=HTMLResponse)
    def table_display(name: str):
        """A Saved or Result Table as the chat's table Display, for the Tables dialog's viewer.
        404 for any other table (Telemetry, Lookup, unknown); 410 once it has expired."""
        try:
            agent.require_live_result_table(name)
        except KeyError:
            raise HTTPException(404, "not a Saved or Result Table")
        return TEMPLATES.get_template("display_table.html").render(**_table_display_urls(name), result_table=name)

    @app.get("/api/tables")
    def list_tables():
        """Names of the tables the user can query, for the Tables dialog. Plain HTTP, never an MCP tool."""
        return agent.list_tables()

    @app.post("/chat", response_class=HTMLResponse)
    def chat(request: Request, message: str = Form(...), conversation_id: str = Form(""), max_rows: str = Form(...)):
        max_result_rows = _parse_max_rows(max_rows)
        if conversation_id:
            try:
                conversation = agent.open_conversation(conversation_id)
            except KeyError:
                raise HTTPException(404, "unknown conversation")
        else:
            conversation = Conversation()
        turn_id = uuid.uuid4().hex
        pending[turn_id] = (conversation, message, max_result_rows)
        if conversation.id is None:  # persist now so the sidebar and form can reference it
            conversation.id = agent.store.create_conversation()
        return TEMPLATES.TemplateResponse(
            request, "turn.html",
            {"turn_id": turn_id, "message": message, **sidebar(conversation.id)},
        )

    @app.get("/chat/stream/{turn_id}")
    async def stream(request: Request, turn_id: str):
        entry = pending.pop(turn_id, None)
        if entry is None:
            raise HTTPException(404)
        conversation, message, max_result_rows = entry

        async def events():
            try:
                async for ev in agent.run_turn(conversation, message, max_result_rows):
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
                    elif isinstance(ev, FlagEvent):
                        yield _sse("flag", TEMPLATES.get_template("flag_note.html").render(flags=ev.items))
                    elif isinstance(ev, TraceReady):
                        if ev.turn_id is not None and conversation.id:
                            yield _sse("turn", TEMPLATES.get_template("turn_delete.html").render(
                                conversation_id=conversation.id, turn_id=ev.turn_id))
                        yield _sse("trace", TEMPLATES.get_template("trace.html").render(trace=ev.trace))
                    elif isinstance(ev, TurnEnd):
                        break
            except Exception as e:  # keep the stream well-formed for the browser
                yield _sse("text", f'<span class="md-delta">{escape(f" [error: {type(e).__name__}]")}</span>')
            yield _sse("end", "done")

        return StreamingResponse(events(), media_type="text/event-stream")

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
            agent.require_live_result_table(result_table)
        except KeyError:
            raise HTTPException(404, "unknown result table")
        return agent.result_table_settings(result_table)

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
