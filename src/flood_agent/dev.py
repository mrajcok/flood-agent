"""Run the app and restart it whenever its files change: uv run flood-agent-dev

Watches the package (code, templates, static files, Canned Prompts, defaults), the
Semantic Layer file and .env. Settings come from the same places as `python -m flood_agent`.
"""

import logging
import os
from pathlib import Path

from watchfiles import run_process

from flood_agent import logs
from flood_agent.config import Settings


def watched_paths(semantic_layer: Path, package_dir: Path = Path(__file__).parent) -> list[Path]:
    """The package directory plus the Semantic Layer and .env files, where they exist."""
    return [package_dir] + [p for p in (semantic_layer, Path(".env")) if p.exists()]


def serve() -> None:
    """The app, run as the child that `main` restarts.

    It leaves the terminal's process group first. Ctrl-C goes to every process in that group,
    and watchfiles also sends the child its own SIGINT when it stops, so a child left in the
    group is interrupted twice and uvicorn prints a traceback on the second. Alone in its own
    group it hears from watchfiles only, once.
    """
    os.setpgrp()
    from flood_agent.__main__ import main as run_app

    run_app(dev=True)


def main() -> None:
    settings = Settings()  # pyright: ignore[reportCallIssue]  # pydantic-settings fills fields from env/TOML
    paths = watched_paths(settings.semantic_layer.resolve())
    log = logging.getLogger("flood_agent.dev")
    log.addHandler(logs.handler())
    log.setLevel(logging.INFO)
    log.propagate = False
    log.info("Watching " + ", ".join(str(p) for p in paths))
    run_process(*paths, target=serve)
