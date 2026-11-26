"""The pre-commit check: ruff, pyright, the non-browser suite, then the browser tests this change calls for.

    uv run scripts/check.py [FEATURE ...] [--base REF] [--dry-run]

Browser tests are picked from the files changed since the merge-base with REF (default origin/main), including
uncommitted and untracked files:
- a change to something every page uses (SHARED below) runs the full browser suite;
- otherwise each changed test file `tests/test_<feature>[_page|_browser].py`, and each FEATURE given, runs
  `tests/test_<feature>*_browser.py`.
Changed source files with no browser tests picked are listed, so you can name their FEATURE.
"""

import argparse
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TEMPLATES = "src/flood_agent/templates/"
SHARED_FILES = {
    f"{TEMPLATES}{name}"
    for name in ("_head.html", "_topbar.html", "_signed_in.html", "_dialogs.html", "notebook.html", "notebook_style.html")
} | {"tests/conftest.py"}
SHARED_DIRS = ("src/flood_agent/static/",)


def git(*args: str) -> list[str]:
    out = subprocess.run(["git", *args], cwd=ROOT, check=True, capture_output=True, text=True).stdout
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


def run(cmd: list[str], dry_run: bool) -> None:
    print("$", " ".join(cmd), flush=True)
    if not dry_run and subprocess.run(cmd, cwd=ROOT).returncode:
        sys.exit(f"check failed: {' '.join(cmd)}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("features", nargs="*", help="extra features whose browser tests to run")
    parser.add_argument("--base", default="origin/main", help="compare against the merge-base with this ref")
    parser.add_argument("--dry-run", action="store_true", help="print what would run")
    args = parser.parse_args()

    changed = changed_files(args.base)
    shared = [path for path in changed if is_shared(path)]
    features = set(args.features) | {f for path in changed if (f := feature_of(path))}

    run(["uv", "run", "ruff", "check", "--fix", "."], args.dry_run)
    run(["uv", "run", "pyright"], args.dry_run)
    run(["uv", "run", "pytest", "-q"], args.dry_run)

    if shared:
        print(f"full browser suite: changed {', '.join(shared)}")
        run(["uv", "run", "pytest", "-q", "-m", "browser", "--browser"], args.dry_run)
        return
    files = browser_files(features)
    if files:
        run(["uv", "run", "pytest", "-q", "--browser", *files], args.dry_run)
    sources = [path for path in changed if path.startswith("src/")]
    if sources and not files:
        print("no browser tests picked for changed sources; name their FEATURE if they have a UI:", *sources, sep="\n  ")


if __name__ == "__main__":
    main()
