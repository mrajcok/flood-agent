"""The pre-commit check: ruff, pyright, the non-browser suite, then the browser tests this change calls for.

    uv run scripts/check.py [FEATURE ...] [--base REF] [--dry-run] [--force]

Browser tests are picked from the files changed since the merge-base with REF (default origin/main), including
uncommitted and untracked files:
- a change to something every page uses (SHARED below) runs the full browser suite;
- otherwise each changed test file `tests/test_<feature>[_page|_browser].py`, and each FEATURE given, runs
  `tests/test_<feature>*_browser.py`.
Changed source files with no browser tests picked are listed, so you can name their FEATURE.

A command that already passed on identical files (same Git tree, counting uncommitted and untracked files) is skipped,
in this or any other worktree: a clean merge often yields a tree that has passed. --force runs it again. Ruff always
runs, because it may fix files.
"""

import argparse
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TEMPLATES = "src/flood_agent/templates/"
SHARED_FILES = {
    f"{TEMPLATES}{name}"
    for name in ("_head.html", "_topbar.html", "_dialogs.html", "notebook.html", "notebook_style.html")
} | {"tests/conftest.py"}
SHARED_DIRS = ("src/flood_agent/static/",)


def git(*args: str, env: dict[str, str] | None = None) -> list[str]:
    out = subprocess.run(["git", *args], cwd=ROOT, env=env, check=True, capture_output=True, text=True).stdout
    return [line for line in out.splitlines() if line]


def changed_files(base: str) -> list[str]:
    merge_base = git("merge-base", base, "HEAD")[0]
    return sorted(set(git("diff", "--name-only", merge_base)) | set(git("ls-files", "--others", "--exclude-standard")))


def is_shared(path: str) -> bool:
    return path in SHARED_FILES or path.startswith(SHARED_DIRS)


def feature_of(path: str) -> str | None:
    p = Path(path)
    if p.parent != Path("tests") or not p.name.startswith("test_") or p.suffix != ".py":
        return None
    name = p.stem.removeprefix("test_")
    for suffix in ("_browser", "_page"):
        name = name.removesuffix(suffix)
    return name


def browser_files(features: set[str]) -> list[str]:
    return sorted({str(f.relative_to(ROOT)) for feature in features for f in (ROOT / "tests").glob(f"test_{feature}*_browser.py")})


def tree_hash() -> str:
    """The Git tree of the files as they are now, built in a scratch index so the real one is left alone."""
    with tempfile.TemporaryDirectory() as tmp:
        index = Path(tmp) / "index"
        shutil.copy(ROOT / git("rev-parse", "--git-path", "index")[0], index)
        env = {**os.environ, "GIT_INDEX_FILE": str(index)}
        git("add", "--all", env=env)
        return git("write-tree", env=env)[0]


def passes_file() -> Path:
    """One `tree<TAB>command` line per pass, in the Git directory all worktrees share."""
    return ROOT / git("rev-parse", "--git-common-dir")[0] / "check-passes"


def run(cmd: list[str], dry_run: bool, tree: str | None = None, force: bool = False) -> None:
    """Run `cmd`, exiting if it fails. Given a `tree`, skip a command that passed on it, and record a new pass."""
    line = " ".join(cmd)
    record = passes_file()
    entry = f"{tree}\t{line}"
    if tree and not force and record.exists() and entry in record.read_text().splitlines():
        print(f"skipped, already passed on these files: {line}", flush=True)
        return
    print("$", line, flush=True)
    if dry_run:
        return
    if subprocess.run(cmd, cwd=ROOT).returncode:
        sys.exit(f"check failed: {line}")
    if tree:
        with record.open("a") as f:
            f.write(entry + "\n")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("features", nargs="*", help="extra features whose browser tests to run")
    parser.add_argument("--base", default="origin/main", help="compare against the merge-base with this ref")
    parser.add_argument("--dry-run", action="store_true", help="print what would run")
    parser.add_argument("--force", action="store_true", help="run commands that already passed on these files")
    args = parser.parse_args()

    changed = changed_files(args.base)
    shared = [path for path in changed if is_shared(path)]
    features = set(args.features) | {f for path in changed if (f := feature_of(path))}

    run(["uv", "run", "ruff", "check", "--fix", "."], args.dry_run)
    tree = tree_hash()  # after ruff, which may have changed files

    def check(cmd: list[str]) -> None:
        run(cmd, args.dry_run, tree, args.force)

    check(["uv", "run", "pyright"])
    check(["uv", "run", "pytest", "-q"])

    if shared:
        print(f"full browser suite: changed {', '.join(shared)}")
        check(["uv", "run", "pytest", "-q", "-m", "browser", "--browser"])
        return
    files = browser_files(features)
    if files:
        check(["uv", "run", "pytest", "-q", "--browser", *files])
    sources = [path for path in changed if path.startswith("src/")]
    if sources and not files:
        print("no browser tests picked for changed sources; name their FEATURE if they have a UI:", *sources, sep="\n  ")


if __name__ == "__main__":
    main()
