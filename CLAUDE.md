# CANOPY

## Checks

Run only the tests for what you changed; the user waits on every run. Test files are named by feature (`test_<feature>.py`, `test_<feature>_page.py`, `test_<feature>_browser.py`), so pick them by glob:

- While iterating: `uv run pytest -q tests/test_<feature>*.py --browser`. Add files for any other feature whose code you touched. Re-run failures with `--lf`.
- Before committing: `uv run ruff check --fix . && uv run pyright && uv run pytest -q`. This is the full non-browser suite (about 20s). Then run the browser tests for the touched features only.
- Run the full browser suite (`uv run pytest -m browser --browser`, about 2 min) only when the user asks, or after changing something every page uses: `_head.html`, `_topbar.html`, `_dialogs.html`, `notebook.html`, `notebook_style.html`, shared JS in `static/`, or `tests/conftest.py`.

## Browser tests

To check UI behaviour (dialogs, Alpine/htmx interactions, layout), write a pytest test using the `live_page` fixture (`tests/conftest.py`): it serves the real app and returns a Playwright `page` in headless system Chrome. Example: `tests/test_dialogs_browser.py`. Such tests are auto-marked `browser` and skipped unless you pass `--browser`. Don't install Playwright browsers or hand-roll a server.

## Agent skills

### Issue tracker

Issues are tracked in GitHub Issues on `mrajcok/flood-agent` via the `gh` CLI. See `docs/agents/issue-tracker.md`.

### Triage labels

Default vocabulary: `needs-triage`, `needs-info`, `ready-for-agent`, `ready-for-human`, `wontfix`. See `docs/agents/triage-labels.md`.

### Domain docs

Single-context: one `GLOSSARY.md` and `docs/adr/` at the repo root. See `docs/agents/domain.md`.
