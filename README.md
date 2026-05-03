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

# 4. Run the dev server (make sure port 8000 is free first)
uv run uvicorn calsync.main:app --reload --port 8000

# 5. Authorize each account in the browser:
#    http://localhost:8000/oauth/start?account=personal
#    http://localhost:8000/oauth/start?account=avela
#    http://localhost:8000/oauth/start?account=beachmedia
#    http://localhost:8000/oauth/start?account=novact
```

### OAuth flow notes

- Use a **separate Chrome profile per Google account**, or open each
  authorization URL in a fresh incognito window and sign in there.
  Sharing one browser session across accounts will silently authorize
  the wrong identity.
- The "Google hasn't verified this app" warning is **expected** while
  the OAuth consent screen is in Testing status. Click
  `Advanced -> Go to calsync (unsafe)` to proceed. To remove the
  warning later, click "Publish App" in Cloud Console (don't submit
  for verification - unverified-published is fine for personal use).
- Subsequent reauthorizations under the same label MUST log in as the
  same email. Logging in as a different email returns 409.

### Verify state after OAuth

```bash
uv run python -c "
from calsync.db import connect
from pathlib import Path
with connect(Path('calsync-dev.db')) as conn:
    for r in conn.execute(
        'SELECT label, email, needs_reauth FROM accounts ORDER BY label'
    ):
        print(dict(r))
"
```

Should show all 4 accounts with the right emails and `needs_reauth: 0`.

## Development

```bash
uv run pytest               # tests
uv run ruff check .         # lint
uv run ruff format .        # format
uv run mypy src/            # type check
```

## Project layout

```
src/calsync/
  config.py        Settings (env vars + YAML config models)
  crypto.py        Fernet token encryption + HMAC mirror_key derivation
  db.py            SQLite connection helpers
  schema.sql       Table definitions and indexes
  oauth.py         OAuth helpers (state signing, code exchange, userinfo)
  keygen.py        `python -m calsync.keygen` -> prints secrets
  deps.py          FastAPI shared dependencies
  main.py          FastAPI app + lifespan
  api/             HTTP routes (oauth, webhook, admin coming)
  repositories/    DB access layer (accounts, more coming)
```

The design plan lives outside this repo at `../calsync-plan/`.

## Status

Phase 1 (scaffolding): OAuth flow, SQLite schema, crypto helpers,
4-account local OAuth all working. Next: Google Calendar API client
wrapper (read paths first), then watch channels, then sync logic.
