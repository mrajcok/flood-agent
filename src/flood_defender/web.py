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

from flood_defender.agent import (
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
from flood_defender.queries import InvalidSort
from flood_defender.trace import split_system_prompt

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


def create_app(agent: Agent, picker: list[PickerEntry] | None = None, fake_mode: bool = False) -> FastAPI:
    """In Fake Mode the page shows a badge and the `picker` list of prompts."""
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        # One long-lived MCP server for the app; a server that can't start fails app startup.
        async with agent:
            yield

    app = FastAPI(title="Flood Defender", lifespan=lifespan)

    @app.exception_handler(ResultTableExpired)
    def expired_handler(request: Request, exc: ResultTableExpired):
        # Any endpoint reading Result Table rows (rows, CSV, chart rows) gets a clear 410.
        return JSONResponse({"detail": "This result has expired and been deleted. Ask again to regenerate it."}, 410)

    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
    pending: dict[str, tuple[Conversation, str]] = {}

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
                    **item, "spec_json": json.dumps(spec), "expired": item["result_table"] in expired,
                })
            elif item["type"] == "display":
                items.append(
                    {
                        **item,
                        "rows_url": f"/api/results/{item['result_table']}/rows",
                        "csv_url": f"/api/results/{item['result_table']}/csv",
                        "expired": item["result_table"] in expired,
                    }
                )
            elif item["type"] == "trace":
                items.append({**item, "flags": [e for e in item["trace"]["entries"] if e["kind"] == "flag"]})
            else:
                m = item["message"]
                if m["role"] in ("user", "assistant") and m.get("content"):
                    items.append({"type": m["role"], "text": m["content"]})
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

    @app.post("/c/{conversation_id}/delete")
    def delete_conversation(conversation_id: str):
        try:
            agent.delete_conversation(conversation_id)
        except KeyError:
            raise HTTPException(404, "unknown conversation")
        return RedirectResponse("/", status_code=303)

    @app.post("/c/{conversation_id}/rename")
    def rename_conversation(conversation_id: str, title: str = Form("")):
        if not agent.store.exists(conversation_id):
            raise HTTPException(404, "unknown conversation")
        title = title.strip()[:60]
        if not title:
            raise HTTPException(422, "a session name cannot be blank")
        agent.store.rename_conversation(conversation_id, title)
        return RedirectResponse(f"/c/{conversation_id}", status_code=303)

    @app.post("/chat", response_class=HTMLResponse)
    def chat(request: Request, message: str = Form(...), conversation_id: str = Form("")):
        if conversation_id:
            try:
                conversation = agent.open_conversation(conversation_id)
            except KeyError:
                raise HTTPException(404, "unknown conversation")
        else:
            conversation = Conversation()
        turn_id = uuid.uuid4().hex
        pending[turn_id] = (conversation, message)
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
        conversation, message = entry

        async def events():
            try:
                async for ev in agent.run_turn(conversation, message):
                    if isinstance(ev, TextDelta):
                        yield _sse("text", escape(ev.text))
                    elif isinstance(ev, DisplayEvent) and ev.kind == "chart":
                        html = TEMPLATES.get_template("display_chart.html").render(
                            spec_json=json.dumps(ev.spec)
                        )
                        yield _sse("display", html)
                    elif isinstance(ev, DisplayEvent):
                        html = TEMPLATES.get_template("display_table.html").render(
                            rows_url=f"/api/results/{ev.result_table}/rows",
                            csv_url=f"/api/results/{ev.result_table}/csv",
                            result_table=ev.result_table,
                            columns=ev.columns,
                        )
                        yield _sse("display", html)
                    elif isinstance(ev, FlagEvent):
                        yield _sse("flag", TEMPLATES.get_template("flag_note.html").render(flags=ev.items))
                    elif isinstance(ev, TraceReady):
                        yield _sse("trace", TEMPLATES.get_template("trace.html").render(trace=ev.trace))
                    elif isinstance(ev, TurnEnd):
                        break
            except Exception as e:  # keep the stream well-formed for the browser
                yield _sse("text", escape(f" [error: {type(e).__name__}]"))
            yield _sse("end", "done")

        return StreamingResponse(events(), media_type="text/event-stream")

    @app.get("/api/results/{result_table}/rows")
    def rows(
        result_table: str,
        page: int = Query(1, ge=1),
        page_size: int = Query(50, ge=1, le=500),
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

    @app.get("/api/results/{result_table}/csv")
    def csv_download(result_table: str):
        try:
            columns, rows_iter = agent.iter_all_rows(result_table)
        except KeyError:
            raise HTTPException(404, "unknown result table")

        def body():
            buf = io.StringIO()
            w = csv.writer(buf, lineterminator="\r\n")
            w.writerow([_csv_safe(c) for c in columns])
            for row in rows_iter:
                w.writerow([_csv_safe(v) for v in row])
                if buf.tell() > 65536:
                    yield buf.getvalue()
                    buf.seek(0)
                    buf.truncate()
            yield buf.getvalue()

        return StreamingResponse(
            body(),
            media_type="text/csv; charset=utf-8",
            headers={"Content-Disposition": f'attachment; filename="{result_table}.csv"'},
        )

    @app.get("/api/results/{result_table}/chart-rows")
    def chart_rows(result_table: str):
        """Internal data path for charts (row objects, chart cap). Never an MCP tool."""
        try:
            return agent.fetch_chart_rows(result_table)
        except KeyError:
            raise HTTPException(404, "unknown result table")

    return app


def _csv_safe(v):
    """Neutralise spreadsheet formula injection in text cells; numbers pass through."""
    if v is None:
        return ""
    if isinstance(v, str) and v[:1] in ("=", "+", "-", "@", "\t", "\r"):
        return "'" + v
    return v
