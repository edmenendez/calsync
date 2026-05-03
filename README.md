# calsync

Self-hosted Google Calendar sync that mirrors events across personal and
work accounts. Personal aggregates full event details from work; work
accounts exchange privacy-preserving "Busy" placeholders.

## Quickstart

```bash
# Install dependencies
uv sync

# 1. Generate secret keys (run once)
uv run python -m calsync.keygen

# 2. Copy the env example and paste the keys into it
cp .env.example .env
# Edit .env: paste CALSYNC_FERNET_KEY, CALSYNC_MIRROR_HMAC_KEY,
#            CALSYNC_ADMIN_TOKEN from step 1.

# 3. Set up Google OAuth client in Cloud Console
# https://console.cloud.google.com/apis/credentials
#   - Enable Google Calendar API
#   - OAuth consent screen: External, add Calendar scope, add yourself as test user
#   - OAuth Client ID: type Web application
#   - Authorized redirect URI: http://localhost:8000/oauth/callback
# Paste the client ID and secret into .env.

# 4. Run the dev server
uv run uvicorn calsync.main:app --reload --port 8000

# 5. Authorize each account in the browser:
#    http://localhost:8000/oauth/start?account=personal
#    http://localhost:8000/oauth/start?account=avela
#    http://localhost:8000/oauth/start?account=beachmedia
#    http://localhost:8000/oauth/start?account=novact
```

## Development

```bash
uv run pytest               # tests
uv run ruff check .         # lint
uv run ruff format .        # format
uv run mypy src/            # type check
```

## Status

Phase 1 (scaffolding): in progress. OAuth flow + SQLite + crypto
helpers in place. Next: Google API client, watch channels, sync logic.
