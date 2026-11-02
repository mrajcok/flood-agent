# flood-agent

## Checks

Before committing: `uv run ruff check --fix . && uv run pyright && uv run pytest -q`.

## Agent skills

### Issue tracker

Issues are tracked in GitHub Issues on `mrajcok/flood-agent` via the `gh` CLI. See `docs/agents/issue-tracker.md`.

### Triage labels

Default vocabulary: `needs-triage`, `needs-info`, `ready-for-agent`, `ready-for-human`, `wontfix`. See `docs/agents/triage-labels.md`.

### Domain docs

Single-context: one `GLOSSARY.md` and `docs/adr/` at the repo root. See `docs/agents/domain.md`.
