"""Layer boundaries (spec #186): Notebook storage is private to the Notebook module and the Run modules, and the web
layer reaches Notebooks only through the Notebook module."""

import ast
from pathlib import Path

from conftest import ScriptedLLM

from flood_agent.notebook_store import NotebookStore

SRC = Path(__file__).parent.parent / "src" / "flood_agent"
STORAGE = "flood_agent.notebook_store"

# Each module allowed to import Notebook storage, and why. Add a module here only with a reason.
STORAGE_IMPORTERS = {
    "agent": "builds storage and hands it to the Notebook module and the Run modules",
    "notebook": "the Notebook module",
    "sql_cell_run": "a Run module",
    "ai_cell_run": "a Run module",
    "cell": "the Cell module saves Drafts and reads the default row cap",
    "mcp_server": "the pure rename_data_source helper",
    "evalset.notebook": "the DEFAULT_MAX_ROWS constant",
}


def imports_of(path: Path) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text())):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module)
            names.update(f"{node.module}.{alias.name}" for alias in node.names)
    return names


def module_name(path: Path) -> str:
    return ".".join(path.relative_to(SRC).with_suffix("").parts)


def test_only_the_listed_modules_import_notebook_storage():
    importers = {
        module_name(path) for path in SRC.rglob("*.py")
        if path.stem != "notebook_store" and STORAGE in imports_of(path)
    }
    assert importers <= STORAGE_IMPORTERS.keys(), f"new importers of {STORAGE}: {importers - STORAGE_IMPORTERS.keys()}"


def test_the_agent_exposes_no_notebook_storage_even_one_step_down(build_agent):
    agent = build_agent(ScriptedLLM())
    for name, value in vars(agent).items():
        if name.startswith("_"):
            continue
        assert not isinstance(value, NotebookStore), f"agent.{name} is Notebook storage"
        for inner, held in getattr(value, "__dict__", {}).items():
            assert inner.startswith("_") or not isinstance(held, NotebookStore), (
                f"agent.{name}.{inner} is Notebook storage"
            )
