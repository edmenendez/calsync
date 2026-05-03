# calsync

Self-hosted Google Calendar sync that mirrors events across personal and work
accounts. Personal aggregates full event details from work; work accounts
exchange privacy-preserving "Busy" placeholders.

## Quickstart

```bash
# Install dependencies (uv-managed)
uv sync

# Run the dev server
uv run uvicorn calsync.main:app --reload --port 8000

# Run tests
uv run pytest

# Lint
uv run ruff check .
uv run ruff format .

# Type-check
uv run mypy src/
```

## Status

Phase 1: scaffolding.
