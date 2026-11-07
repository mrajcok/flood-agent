"""The dev entry point watches the files that make up the app."""

import subprocess
import sys
from pathlib import Path

import pytest

from flood_agent.dev import watched_paths


@pytest.fixture(autouse=True)
def _in_tmp_dir(tmp_path, monkeypatch):
    """`.env` is looked up in the working directory: keep a developer's own .env out of these tests."""
    monkeypatch.chdir(tmp_path)


def test_watches_the_package_and_the_semantic_layer(tmp_path):
    layer = tmp_path / "layer.yaml"
    layer.write_text("tables: {}")
    package = tmp_path / "pkg"
    package.mkdir()

    assert watched_paths(layer, package) == [package, layer]


def test_skips_files_that_do_not_exist(tmp_path):
    package = tmp_path / "pkg"
    package.mkdir()

    assert watched_paths(tmp_path / "missing.yaml", package) == [package]


def test_watches_dot_env_when_there_is_one(tmp_path):
    layer = tmp_path / "layer.yaml"
    layer.write_text("tables: {}")
    (tmp_path / ".env").write_text("")
    package = tmp_path / "pkg"
    package.mkdir()

    assert watched_paths(layer, package) == [package, layer, Path(".env")]


def test_the_app_child_runs_in_its_own_process_group():
    """Ctrl-C reaches the terminal's whole process group, and watchfiles also signals the child
    itself: a child left in that group is interrupted twice and prints a traceback."""
    code = (
        "import os, flood_agent.__main__ as app, flood_agent.dev as dev\n"
        "app.main = lambda: print(os.getpgrp() == os.getpid())\n"
        "dev.serve()\n"
    )
    result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=30)

    assert result.stdout.strip() == "True", result.stderr
