"""Smoke check that prompt caching works against the real OpenRouter API, through a notebook with two AI Cells.

    export OPENROUTER_API_KEY=...   # real, paid calls
    uv run python -m flood_agent.evalset.smoke_caching [--model anthropic/claude-sonnet-5.5]

AI Cell 1 asks one golden question that needs a tool call, so it makes two LLM steps seconds
apart. Step 2 re-sends step 1's prompt, so a working cache shows cache-write tokens on step 1 and
cached (read) tokens on step 2. AI Cell 2 (`--question2`) then runs in the same notebook: every cell of a
notebook shares the session id `notebook-<id>`, and its first request must start, byte for byte, with AI
Cell 1's last request, so it reads the cache AI Cell 1 wrote. The script prints each step's usage as the app
parsed it, the raw usage object the provider sent, and the caching fields the request carried, then a verdict
for each of the two checks.

Costs a few cents (four calls). Exit status: 0 working, 1 not working, 2 inconclusive or setup error.
Never part of CI or default pytest; the verdict logic and the harness are tested offline against a simulated
provider (tests/test_smoke_caching.py).
"""

import argparse
import asyncio
import json
import sys
from dataclasses import dataclass, field, replace
from pathlib import Path

import httpx
from pydantic import ValidationError

from flood_agent.agent import Agent, TextDelta, TurnError
from flood_agent.config import Settings
from flood_agent.evalset.golden import Question, load_golden
from flood_agent.evalset.notebook import add_cells_above, run_ai_cell
from flood_agent.llm import OpenRouterClient, UsageChunk

DEFAULT_MODEL = "anthropic/claude-sonnet-5.5"
DEFAULT_QUESTION = "total-flow-count"
# AI Cell 2, in the same notebook, after AI Cell 1. Neither question may make a Result Table: a new one changes
# the end of the system prompt (ADR 0007), so cell 2's request would no longer start with cell 1's.
DEFAULT_QUESTION2 = "cells-ticked-note-narrows"
MIN_CACHEABLE_TOKENS = 512  # OpenRouter: the 5.5 Claude models cache prompts of at least this many tokens


@dataclass
class Step:
    """One LLM call: usage as the app parsed it, plus what was sent and the raw usage object."""

    prompt_tokens: int = 0
    cached_tokens: int = 0
    cache_write_tokens: int = 0
    cost: float | None = None
    sent: dict = field(default_factory=dict)  # the caching fields the request body carried
    raw_usage: dict | None = None  # the provider's usage object, unparsed
    provider: str | None = None  # the upstream provider OpenRouter routed the call to; caches are per provider
    body: dict | None = None  # the request body, to compare one step's prompt with the next's


def prefix_difference(first: dict, second: dict) -> str | None:
    """None if `second`'s tools and messages start with exactly `first`'s (so a cache could be reused), else where they differ."""
    if first.get("tools") != second.get("tools"):
        return "the tool definitions differ"
    a, b = first.get("messages", []), second.get("messages", [])
    for i, message in enumerate(a):
        if i >= len(b) or b[i] != message:
            return f"message {i} (role {message.get('role')!r}) differs"
    return None


def judge(steps: list[Step], expects_cache_control: bool) -> tuple[str, str]:
    """(verdict, explanation): verdict is WORKING, NOT WORKING or INCONCLUSIVE."""
    if len(steps) < 2:
        return "INCONCLUSIVE", f"the turn made {len(steps)} LLM call(s); a cache read needs a second step. Try another question."
    first, second = steps[0], steps[1]
    if first.prompt_tokens < MIN_CACHEABLE_TOKENS:
        return "INCONCLUSIVE", (f"step 1's prompt was {first.prompt_tokens} tokens, under the "
                                f"{MIN_CACHEABLE_TOKENS}-token minimum, so no cache would be written.")
    if expects_cache_control and "cache_control" not in first.sent:
        return "NOT WORKING", "the request carried no cache_control, so a Claude model cannot cache (a bug in the app)."
    if second.cached_tokens > 0:
        note = f"step 2 read {second.cached_tokens} cached tokens of {second.prompt_tokens}"
        if first.cache_write_tokens:
            note += f" after step 1 wrote {first.cache_write_tokens}"
        elif first.cached_tokens:
            note += f"; step 1 already read {first.cached_tokens} (the cache was warm from an earlier run within its TTL)"
        else:
            note += "; step 1 reported no cache write (the provider may not report writes)"
        return "WORKING", note + "."
    details = (second.raw_usage or {}).get("prompt_tokens_details")
    if second.raw_usage is None:
        why = "the response carried no usage object"
    elif details is None:
        why = "the provider's usage has no prompt_tokens_details, so it reports no cache fields at all"
    elif first.cache_write_tokens:
        why = "step 1 wrote the cache but step 2 did not read it (the prefix differed, or the cache expired or was routed elsewhere)"
    else:
        why = "the usage reports cache fields but both were 0: caching was not applied"
    return "NOT WORKING", f"step 2 read no cached tokens; {why}."


def judge_across_cells(first_cell: list[Step], second_cell: list[Step]) -> tuple[str, str]:
    """(verdict, explanation) for the cache across cells: AI Cell 2's first request must start with AI Cell 1's
    last request, byte for byte, under the same session id, and so read the cache AI Cell 1 wrote."""
    if not first_cell or not second_cell:
        return "INCONCLUSIVE", "a cell made no LLM call."
    last, first = first_cell[-1], second_cell[0]
    difference = prefix_difference(last.body or {}, first.body or {})
    if difference:
        hint = (" (a Result Table made since changes the end of the system prompt, ADR 0007: the cells of this check "
                "must make none)") if "'system'" in difference else ""
        return "NOT WORKING", f"AI Cell 2's first request does not start with AI Cell 1's last request: {difference}{hint}."
    if last.sent.get("session_id") != first.sent.get("session_id"):
        return "NOT WORKING", (f"the cells' session ids differ ({last.sent.get('session_id')!r} vs "
                               f"{first.sent.get('session_id')!r}), so they may be routed to different caches.")
    if last.prompt_tokens < MIN_CACHEABLE_TOKENS:
        return "INCONCLUSIVE", f"AI Cell 1's last prompt was {last.prompt_tokens} tokens, under the {MIN_CACHEABLE_TOKENS}-token minimum."
    if first.cached_tokens > 0:
        return "WORKING", f"AI Cell 2's first request read {first.cached_tokens} cached tokens of {first.prompt_tokens}."
    return "NOT WORKING", "AI Cell 2's first request shares AI Cell 1's last prompt but read no cached tokens."


class _TeeStream(httpx.AsyncByteStream):
    """Passes the response bytes through and keeps the last `usage` object of the event stream."""

    def __init__(self, inner: httpx.AsyncByteStream, call: Step):
        self.inner, self.call, self.buffer = inner, call, b""

    async def __aiter__(self):
        async for chunk in self.inner:
            self.buffer += chunk
            yield chunk

    def _find_usage(self) -> None:
        for line in self.buffer.decode(errors="replace").splitlines():
            if not line.startswith("data:"):
                continue
            try:
                chunk = json.loads(line[5:])
            except ValueError:
                continue
            if isinstance(chunk, dict):
                self.call.provider = self.call.provider or chunk.get("provider")
                if chunk.get("usage"):
                    self.call.raw_usage = chunk["usage"]

    async def aclose(self) -> None:  # the client stops reading at [DONE], so the stream may never run to its end
        self._find_usage()
        await self.inner.aclose()


def apply_experiment(body: dict, provider: str | None, cache_at: str) -> dict:
    """The request body with an experiment applied; the app's own requests are left as they are.

    `provider` pins OpenRouter to that upstream provider with no fallback. `cache_at="system"` drops the
    top-level cache_control and puts it on the system message's content block instead."""
    body = json.loads(json.dumps(body))
    if provider:
        body["provider"] = {"order": [provider], "allow_fallbacks": False}
    if cache_at == "system":
        body.pop("cache_control", None)
        system = body["messages"][0]
        if system["role"] == "system" and isinstance(system["content"], str):
            system["content"] = [{"type": "text", "text": system["content"], "cache_control": {"type": "ephemeral"}}]
    return body


class _Tee(httpx.AsyncBaseTransport):
    """Records, per request, the caching fields sent and the raw usage received; can apply an experiment first."""

    def __init__(self, provider: str | None = None, cache_at: str = "top", delay: float = 0,
                 inner: httpx.AsyncBaseTransport | None = None):
        self.inner = inner or httpx.AsyncHTTPTransport()  # tests pass a simulated provider
        self.calls: list[Step] = []
        self.provider, self.cache_at, self.delay = provider, cache_at, delay

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        if self.calls and self.delay:
            await asyncio.sleep(self.delay)  # experiment: give a just-written cache time to become readable
        body = apply_experiment(json.loads(request.content), self.provider, self.cache_at)
        sent = {k: body[k] for k in ("cache_control", "session_id", "provider") if k in body}
        if self.cache_at == "system":
            sent["cache_control"] = "on the system message block"
        call = Step(sent=sent, body=body)
        self.calls.append(call)
        headers = {k: v for k, v in request.headers.items() if k.lower() not in ("content-length", "accept-encoding")}
        request = httpx.Request(request.method, request.url, headers={**headers, "accept-encoding": "identity"},
                                content=json.dumps(body).encode())  # identity: so the tee reads plain text
        response = await self.inner.handle_async_request(request)
        return httpx.Response(response.status_code, headers=response.headers, extensions=response.extensions,
                              stream=_TeeStream(response.stream, call))  # pyright: ignore[reportArgumentType]


class _Recorder:
    """Wraps the real client; records each step's parsed UsageChunk."""

    def __init__(self, inner: OpenRouterClient):
        self.inner, self.usages = inner, []

    async def stream_chat(self, messages, tools, reasoning=None, session_id=None):
        async for chunk in self.inner.stream_chat(messages, tools, reasoning=reasoning, session_id=session_id):
            if isinstance(chunk, UsageChunk):
                self.usages.append(chunk)
            yield chunk


async def run_notebook(model: str, questions: list[Question], settings: Settings, workdir: Path,
                       provider: str | None = None, cache_at: str = "top", delay: float = 0,
                       inner: httpx.AsyncBaseTransport | None = None) -> tuple[list[list[Step]], str | None, str]:
    """Run each question as an AI Cell of one new notebook (with the question's cells above run first),
    against the real API unless `inner` simulates the provider. Returns each AI Cell's steps, the first
    error of any cell, and the last cell's reply text."""
    key = settings.openrouter_api_key
    assert key is not None
    tee = _Tee(provider, cache_at, delay, inner)
    recorder = _Recorder(OpenRouterClient(key.get_secret_value(), model, transport=tee))
    workdir.mkdir(parents=True, exist_ok=True)
    results_db, app_db = workdir / "results.db", workdir / "app.db"
    for p in (results_db, app_db):
        p.unlink(missing_ok=True)
    agent = Agent(replace(settings.agent_config(), model=model, results_db=results_db, app_db=app_db),
                  lambda _model: recorder)
    error, reply, cells = None, "", []
    async with agent:
        notebook_id = agent.create_notebook("caching smoke check")
        for q in questions:
            await add_cells_above(agent, notebook_id, q.above)
            first_call = len(tee.calls)
            reply = ""
            _, events = await run_ai_cell(agent, notebook_id, q.question, 1000)
            async for event in events:
                if isinstance(event, TurnError):
                    error = error or event.detail
                elif isinstance(event, TextDelta):
                    reply += event.text
            steps = []
            for call, usage in zip(tee.calls[first_call:], recorder.usages[first_call:], strict=False):
                call.prompt_tokens, call.cached_tokens = usage.prompt_tokens or 0, usage.cached_tokens or 0
                call.cache_write_tokens, call.cost = usage.cache_write_tokens or 0, usage.cost
                steps.append(call)
            cells.append(steps)
    return cells, error, reply


def tool_activity(steps: list[Step]) -> str:
    """What the later requests added to the conversation: the model's tool calls and the tool results fed back."""
    if len(steps) < 2 or not steps[0].body or not steps[-1].body:
        return "no tool activity (the turn made fewer than 2 LLM calls)"
    added = steps[-1].body["messages"][len(steps[0].body["messages"]):]
    parts = []
    for m in added:
        if m.get("role") == "assistant":
            calls = [c["function"]["name"] for c in m.get("tool_calls") or []]
            parts.append(f"assistant called {', '.join(calls)}" if calls else "assistant text")
        elif m.get("role") == "tool":
            parts.append(f"tool result ({len(str(m.get('content')))} chars)")
    return "; ".join(parts) or "no messages added"


def render(steps: list[Step]) -> str:
    lines = []
    for i, s in enumerate(steps, 1):
        cost = "n/a" if s.cost is None else f"${s.cost:.5f}"
        lines += [f"step {i}: prompt {s.prompt_tokens}  cached {s.cached_tokens}  cache write {s.cache_write_tokens}  cost {cost}  provider {s.provider}",
                  f"        request carried: {s.sent or 'no caching fields'}",
                  f"        raw usage: {json.dumps(s.raw_usage)}"]
    if len(steps) >= 2 and steps[0].body and steps[1].body:
        same = prefix_difference(steps[0].body, steps[1].body)
        lines.append("step 2 starts with step 1's exact prompt: " + ("yes" if same is None else f"NO, {same}"))
    return "\n".join(lines)


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="Smoke-check prompt caching within an AI Cell and across the cells of a notebook.")
    p.add_argument("--model", default=DEFAULT_MODEL)
    p.add_argument("--question", default=DEFAULT_QUESTION, help="golden question id for AI Cell 1; it must need a tool call")
    p.add_argument("--question2", default=DEFAULT_QUESTION2, help="golden question id for AI Cell 2 (its cells above run first)")
    p.add_argument("--provider", help="experiment: pin OpenRouter to this upstream provider (e.g. Anthropic), no fallback")
    p.add_argument("--cache-at", choices=["top", "system"], default="top",
                   help="experiment: 'system' moves cache_control from the request's top level onto the system message block")
    p.add_argument("--delay", type=float, default=0,
                   help="experiment: seconds to wait before every request after the first, so a new cache entry can become readable")
    p.add_argument("--workdir", default="data/smoke-work", help="scratch results/app databases")
    a = p.parse_args(argv)
    try:
        settings = Settings()  # pyright: ignore[reportCallIssue]  # pydantic-settings fills fields from env/TOML
    except ValidationError as e:
        print(f"invalid settings:\n{e}", file=sys.stderr)
        return 2
    if settings.openrouter_api_key is None:
        print("OPENROUTER_API_KEY is not set; this calls the real OpenRouter API.", file=sys.stderr)
        return 2
    questions = {q.id: q for q in load_golden()}
    for qid in (a.question, a.question2):
        if qid not in questions:
            print(f"unknown question id {qid!r}", file=sys.stderr)
            return 2
    cells, error, reply = asyncio.run(run_notebook(
        a.model, [questions[a.question], questions[a.question2]], settings, Path(a.workdir), a.provider, a.cache_at, a.delay))
    print(f"model {a.model}, questions {a.question} then {a.question2}, provider {a.provider or 'any'}, "
          f"cache_control at {a.cache_at}, delay {a.delay}s")
    for n, steps in enumerate(cells, 1):
        print(f"\nAI Cell {n}\n{render(steps)}\ntool activity between the steps: {tool_activity(steps)}")
    print(f"\nlast reply: {reply.strip()[:300]}")
    if error:
        print(f"\na turn failed: {error}")
    within, why = judge(cells[0], expects_cache_control=a.model.startswith("anthropic/"))
    print(f"\nwithin AI Cell 1, {within}: {why}")
    across, why2 = judge_across_cells(cells[0], cells[1])
    print(f"across cells, {across}: {why2}")
    verdict = "NOT WORKING" if "NOT WORKING" in (within, across) else "INCONCLUSIVE" if "INCONCLUSIVE" in (within, across) else "WORKING"
    return {"WORKING": 0, "NOT WORKING": 1}.get(verdict, 2)


if __name__ == "__main__":
    sys.exit(main())
