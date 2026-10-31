"""FastAPI adapter: server-rendered Jinja + htmx + Alpine, streaming over SSE.

A thin layer over `Agent.run_turn`. The row-JSON endpoint is the Agent's internal data
path to the browser; it is a plain HTTP route, never registered as an MCP tool.
"""

import json
import uuid
from html import escape
from pathlib import Path

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, StreamingResponse
from fastapi.templating import Jinja2Templates

from flood_defender.agent import Agent, Conversation, DisplayEvent, TextDelta, TurnEnd, resolve_chart_data

TEMPLATES = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))


def _sse(event: str, data: str) -> str:
    lines = "".join(f"data: {line}\n" for line in data.split("\n"))
    return f"event: {event}\n{lines}\n"


def create_app(agent: Agent) -> FastAPI:
    app = FastAPI(title="Flood Defender")
    pending: dict[str, tuple[Conversation, str]] = {}

    def sidebar(current: str | None) -> dict:
        return {"conversations": agent.store.list_conversations(), "current_id": current}

    def history(conversation_id: str) -> list[dict]:
        items = []
        for item in agent.store.transcript(conversation_id):
            if item["type"] == "display" and item["kind"] == "chart":
                spec = resolve_chart_data(item["spec"], item["result_table"])
                items.append({**item, "spec_json": json.dumps(spec)})
            elif item["type"] == "display":
                items.append({**item, "rows_url": f"/api/results/{item['result_table']}/rows"})
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
            conversation.id = agent.store.create_conversation(message[:60])
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
                            result_table=ev.result_table,
                            columns=ev.columns,
                        )
                        yield _sse("display", html)
                    elif isinstance(ev, TurnEnd):
                        break
            except Exception as e:  # keep the stream well-formed for the browser
                yield _sse("text", escape(f" [error: {type(e).__name__}]"))
            yield _sse("end", "done")

        return StreamingResponse(events(), media_type="text/event-stream")

    @app.get("/api/results/{result_table}/rows")
    def rows(result_table: str):
        try:
            return agent.fetch_rows(result_table)
        except KeyError:
            raise HTTPException(404, "unknown result table")

    @app.get("/api/results/{result_table}/chart-rows")
    def chart_rows(result_table: str):
        """Internal data path for charts (row objects, chart cap). Never an MCP tool."""
        try:
            return agent.fetch_chart_rows(result_table)
        except KeyError:
            raise HTTPException(404, "unknown result table")

    return app
