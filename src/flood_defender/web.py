"""FastAPI adapter: server-rendered Jinja + htmx + Alpine, streaming over SSE.

A thin layer over `Agent.run_turn`. The row-JSON endpoint is the Agent's internal data
path to the browser; it is a plain HTTP route, never registered as an MCP tool.
"""

import uuid
from html import escape
from pathlib import Path

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, StreamingResponse
from fastapi.templating import Jinja2Templates

from flood_defender.agent import Agent, Conversation, DisplayEvent, TextDelta, TurnEnd

TEMPLATES = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))


def _sse(event: str, data: str) -> str:
    lines = "".join(f"data: {line}\n" for line in data.split("\n"))
    return f"event: {event}\n{lines}\n"


def create_app(agent: Agent) -> FastAPI:
    app = FastAPI(title="Flood Defender")
    conversation = Conversation()  # single user, single in-memory Conversation for now
    pending: dict[str, str] = {}

    @app.get("/", response_class=HTMLResponse)
    def index(request: Request):
        return TEMPLATES.TemplateResponse(request, "index.html", {})

    @app.post("/chat", response_class=HTMLResponse)
    def chat(request: Request, message: str = Form(...)):
        turn_id = uuid.uuid4().hex
        pending[turn_id] = message
        return TEMPLATES.TemplateResponse(
            request, "turn.html", {"turn_id": turn_id, "message": message}
        )

    @app.get("/chat/stream/{turn_id}")
    async def stream(request: Request, turn_id: str):
        message = pending.pop(turn_id, None)
        if message is None:
            raise HTTPException(404)

        async def events():
            try:
                async for ev in agent.run_turn(conversation, message):
                    if isinstance(ev, TextDelta):
                        yield _sse("text", escape(ev.text))
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

    return app
