# Coding standards

Judgement calls for review. Mechanical rules live in `ruff` and `pyright` (config in `pyproject.toml`); this file holds what no tool can check.

## Async resource lifecycles

Anything that starts a subprocess or a task (a `start()`, a restart path) owns its cleanup on every exit, including the start itself being cancelled or failing: catch `BaseException`, signal stop, await the owner, re-raise. A half-started resource is reaped before the error propagates.

A lifecycle object rejects reuse: a second `start()`, or `start()` after `aclose()`, raises a clear error.

Closing an owner (the Agent, the app lifespan) cancels and awaits the work it started before releasing what that work uses.
