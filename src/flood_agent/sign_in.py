"""Sign-in: the page, the sessions, and the gate every request passes.

Sessions are held in memory, so a restart signs everyone out and the app runs as one process.
"""

import logging
import secrets
import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from urllib.parse import quote, urlencode, urlsplit

from fastapi import FastAPI, Form, Request
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from starlette.responses import Response
from starlette.types import ASGIApp, Receive, Scope, Send

from flood_agent.users import PasswordHash

COOKIE = "canopy_session"
SIGN_IN_PATH, SIGN_OUT_PATH = "/sign-in", "/sign-out"
STATIC_PREFIX = "/static/"
WRONG = "User name or password is wrong"

log = logging.getLogger("uvicorn.error")  # the app's terminal log


@dataclass(frozen=True)
class SignInConfig:
    """Who can sign in (`users`, by name; at least one), how long an idle session lasts, and whether its cookie is
    sent over HTTPS only."""

    users: Mapping[str, PasswordHash]
    idle_timeout_ms: int
    cookie_secure: bool

    def __post_init__(self) -> None:
        if not self.users:
            raise ValueError("sign-in needs at least one User")


@dataclass
class _Session:
    user: str
    active_ms: int  # when it last had a request


class Sessions:
    """The live sessions, by token. Safe to use from the event loop and from worker threads."""

    def __init__(self, idle_timeout_ms: int, clock: Callable[[], int]):
        self._idle_timeout_ms, self._clock = idle_timeout_ms, clock
        self._sessions: dict[str, _Session] = {}
        self._lock = threading.Lock()

    def _idle(self, session: _Session, now: int) -> bool:
        return now - session.active_ms > self._idle_timeout_ms

    def start(self, user: str) -> str:
        """A new session for `user`; returns its token (256 random bits)."""
        token = secrets.token_urlsafe(32)
        with self._lock:
            now = self._clock()
            self._sessions = {t: s for t, s in self._sessions.items() if not self._idle(s, now)}
            self._sessions[token] = _Session(user, now)
        return token

    def user_of(self, token: str) -> str | None:
        """The User of a live session, which this counts as activity; None if there is no such session."""
        with self._lock:
            session = self._sessions.get(token)
            if session is None:
                return None
            now = self._clock()
            if self._idle(session, now):
                del self._sessions[token]
                return None
            session.active_ms = now
            return session.user

    def end(self, token: str) -> None:
        with self._lock:
            self._sessions.pop(token, None)


def _same_origin(request: Request) -> bool:
    """Whether the request's Origin, or failing that its Referer, is the app's own scheme and host."""
    source = request.headers.get("origin")
    if source is None:
        source = request.headers.get("referer", "")
    try:
        parts = urlsplit(source)
    except ValueError:
        return False
    own = (request.url.scheme, request.headers.get("host", "").lower())
    return (parts.scheme, parts.netloc.lower()) == own


def _local_path(target: str) -> str:
    """`target` if it is a path on this app, else "/" (the Notebook list). Browsers read `//host` and `/\\host` as
    another site, and drop tabs and newlines before reading, so none of those pass."""
    local = (target.startswith("/") and not target.startswith("//") and "\\" not in target
             and all(" " < c != "\x7f" for c in target))
    return target if local else "/"


def _refusal(request: Request) -> Response:
    """The answer to a request without a session: htmx is told to leave the page, a page is redirected (remembering
    where it was going), and anything else (the page's own fetches, the API) gets 401."""
    detail = {"detail": "sign in first"}
    if "hx-request" in request.headers:
        return JSONResponse(detail, 401, headers={"HX-Redirect": SIGN_IN_PATH})
    path = request.scope["path"]
    reading = request.method in ("GET", "HEAD")
    # A page: a Notebook or the home page, or anything the browser navigates to (a submitted form included).
    page = reading and (path == "/" or (path.startswith("/n/") and path.count("/") == 2))
    page = page or "text/html" in request.headers.get("accept", "")
    if not page or path.startswith("/api/"):
        return JSONResponse(detail, 401)
    if not reading:  # a form cannot be posted again after signing in
        return RedirectResponse(SIGN_IN_PATH, 303)
    target = quote(path) + (f"?{request.url.query}" if request.url.query else "")
    return RedirectResponse(f"{SIGN_IN_PATH}?{urlencode({'next': target}, safe='/')}", 303)


class _Gate:
    """Lets a request through only from the app's own origin (when it can change something) and, apart from the
    sign-in page and static assets, only with a session, whose User it puts in `request.state.user`. Plain ASGI, so
    that streamed responses pass untouched."""

    def __init__(self, app: ASGIApp, sessions: Sessions):
        self.app, self.sessions = app, sessions

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "lifespan":
            return await self.app(scope, receive, send)
        if scope["type"] != "http":  # the app has no websockets
            return await send({"type": "websocket.close"})
        request = Request(scope)
        if request.method not in ("GET", "HEAD") and not _same_origin(request):
            return await JSONResponse({"detail": "request from another origin"}, 403)(scope, receive, send)
        path = scope["path"]
        if path != SIGN_IN_PATH and not path.startswith(STATIC_PREFIX):
            user = self.sessions.user_of(request.cookies.get(COOKIE, ""))
            if user is None:
                return await _refusal(request)(scope, receive, send)
            request.state.user = user
        await self.app(scope, receive, send)


def install(app: FastAPI, config: SignInConfig, clock: Callable[[], int], templates: Jinja2Templates) -> None:
    """Require sign-in of every request to `app`, and add the sign-in page and Sign out."""
    sessions = Sessions(config.idle_timeout_ms, clock)
    # Checked for an unknown name, so that it takes as long as a wrong password.
    decoy = next(iter(config.users.values())).decoy()
    cookie = {"path": "/", "httponly": True, "samesite": "lax", "secure": config.cookie_secure}

    def page(request: Request, next_path: str, name: str = "", error: str = ""):
        return templates.TemplateResponse(
            request, "sign_in.html", {"next": next_path, "name": name, "error": error}, 401 if error else 200)

    @app.get(SIGN_IN_PATH)
    def sign_in_page(request: Request, next: str = ""):
        return page(request, next)

    @app.post(SIGN_IN_PATH)
    def sign_in(request: Request, name: str = Form(""), password: str = Form(""), next: str = Form("")):
        known = config.users.get(name)
        if not (known or decoy).verify(password) or known is None:
            log.warning("Failed sign-in for User %r", name)
            return page(request, next, name, WRONG)
        response = RedirectResponse(_local_path(next), 303)
        response.set_cookie(COOKIE, sessions.start(name), **cookie)
        return response

    @app.post(SIGN_OUT_PATH)
    def sign_out(request: Request):
        sessions.end(request.cookies.get(COOKIE, ""))
        response = RedirectResponse(SIGN_IN_PATH, 303)
        response.delete_cookie(COOKIE, **cookie)
        return response

    app.add_middleware(_Gate, sessions=sessions)
