# flood-agent

## Checks

Before committing: `uv run ruff check --fix . && uv run pyright && uv run pytest -q`.

## Browser tests

To check UI behaviour (dialogs, Alpine/htmx interactions, layout), write a pytest test using the `live_page` fixture (`tests/conftest.py`): it serves the real app and returns a Playwright `page` in headless system Chrome. Example: `tests/test_dialogs_browser.py`. Such tests are auto-marked `browser` and skipped unless you pass `--browser`: run them with `uv run pytest -m browser --browser` after any UI change. Don't install Playwright browsers or hand-roll a server.

## Agent skills

### Issue tracker

Issues are tracked in GitHub Issues on `mrajcok/flood-agent` via the `gh` CLI. See `docs/agents/issue-tracker.md`.

### Triage labels

Default vocabulary: `needs-triage`, `needs-info`, `ready-for-agent`, `ready-for-human`, `wontfix`. See `docs/agents/triage-labels.md`.

### Domain docs

Single-context: one `GLOSSARY.md` and `docs/adr/` at the repo root. See `docs/agents/domain.md`.
