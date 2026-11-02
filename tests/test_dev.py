"""The dev entry point watches the files that make up the app."""

from pathlib import Path

from flood_agent.dev import watched_paths


def test_watches_the_package_and_the_semantic_layer(tmp_path):
    layer = tmp_path / "layer.yaml"
    layer.write_text("tables: {}")
    package = tmp_path / "pkg"
    package.mkdir()

    assert watched_paths(layer, package) == [package, layer]


def test_skips_files_that_do_not_exist(tmp_path):
    package = tmp_path / "pkg"
    package.mkdir()

    assert watched_paths(tmp_path / "missing.yaml", package) == [package, *([Path(".env")] if Path(".env").exists() else [])]
