"""Run the app and restart it whenever its files change: uv run flood-agent-dev

Watches the package (code, templates, static files, Canned Prompts, defaults), the
Semantic Layer file and .env. Settings come from the same places as `python -m flood_agent`.
"""

import sys
from pathlib import Path

from watchfiles import run_process

from flood_agent.config import Settings


def watched_paths(semantic_layer: Path, package_dir: Path = Path(__file__).parent) -> list[Path]:
    """The package directory plus the Semantic Layer and .env files, where they exist."""
    return [package_dir] + [p for p in (semantic_layer, Path(".env")) if p.exists()]


def main() -> None:
    settings = Settings()  # pyright: ignore[reportCallIssue]  # pydantic-settings fills fields from env/TOML
    paths = watched_paths(settings.semantic_layer.resolve())
    print("Watching " + ", ".join(str(p) for p in paths), flush=True)
    run_process(*paths, target=f"{sys.executable} -m flood_agent")
