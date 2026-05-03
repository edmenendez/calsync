"""Per-account access token management.

Two functions:

- `refresh_access_token()` - pure HTTP exchange, no DB.
- `ensure_access_token()` - returns a current access_token for an account,
  refreshing if needed and persisting the new token to the DB.

Sets `accounts.needs_reauth = 1` and `last_error` on `invalid_grant`.
"""

import datetime as dt
import sqlite3

import httpx

from calsync.crypto import decrypt_token, encrypt_token
from calsync.gapi.errors import NeedsReauthError

GOOGLE_TOKEN_URL = 'https://oauth2.googleapis.com/token'

# Refresh proactively when this many seconds remain on the access_token.
ACCESS_TOKEN_REFRESH_BUFFER_SECONDS = 60


async def refresh_access_token(*, refresh_token: str, client_id: str, client_secret: str) -> dict:
    """Exchange a refresh_token for a new access_token via Google's token endpoint.

    Raises NeedsReauthError if Google returns 400 invalid_grant. Other HTTP
    errors propagate as httpx.HTTPStatusError.
    """
    async with httpx.AsyncClient(timeout=10.0) as client:
        r = await client.post(
            GOOGLE_TOKEN_URL,
            data={
                'refresh_token': refresh_token,
                'client_id': client_id,
                'client_secret': client_secret,
                'grant_type': 'refresh_token',
            },
        )
        if r.status_code == 400:
            data = r.json()
            if data.get('error') == 'invalid_grant':
                raise NeedsReauthError(data.get('error_description', 'invalid_grant'))
        r.raise_for_status()
        return r.json()


async def ensure_access_token(
    conn: sqlite3.Connection,
    account_id: int,
    *,
    fernet_key: str,
    client_id: str,
    client_secret: str,
) -> str:
    """Return a valid access_token for the account, refreshing if needed.

    On NeedsReauthError, marks the account in DB and re-raises.
    """
    row = conn.execute(
        'SELECT refresh_token_encrypted, access_token, access_token_expires_at, needs_reauth '
        'FROM accounts WHERE id = ?',
        (account_id,),
    ).fetchone()
    if row is None:
        raise ValueError(f'account {account_id} not found')
    if row['needs_reauth']:
        raise NeedsReauthError(f'account {account_id} flagged needs_reauth; re-authorize via /oauth/start')

    expires_at = dt.datetime.fromisoformat(row['access_token_expires_at']) if row['access_token_expires_at'] else None
    now = dt.datetime.now(dt.UTC)
    if expires_at and row['access_token'] and (expires_at - now).total_seconds() > ACCESS_TOKEN_REFRESH_BUFFER_SECONDS:
        return row['access_token']

    refresh_token = decrypt_token(row['refresh_token_encrypted'], fernet_key)
    try:
        tokens = await refresh_access_token(
            refresh_token=refresh_token,
            client_id=client_id,
            client_secret=client_secret,
        )
    except NeedsReauthError as e:
        conn.execute(
            'UPDATE accounts SET needs_reauth = 1, last_error = ?, last_error_at = ? WHERE id = ?',
            (str(e), dt.datetime.now(dt.UTC).isoformat(), account_id),
        )
        raise

    new_at = tokens['access_token']
    new_expires = dt.datetime.now(dt.UTC) + dt.timedelta(seconds=int(tokens.get('expires_in', 3600)))

    # Google occasionally rotates refresh tokens; persist the new one if returned.
    new_rt = tokens.get('refresh_token')
    if new_rt:
        encrypted = encrypt_token(new_rt, fernet_key)
        conn.execute(
            'UPDATE accounts SET refresh_token_encrypted = ?, access_token = ?, '
            'access_token_expires_at = ?, last_error = NULL, last_error_at = NULL WHERE id = ?',
            (encrypted, new_at, new_expires.isoformat(), account_id),
        )
    else:
        conn.execute(
            'UPDATE accounts SET access_token = ?, access_token_expires_at = ?, '
            'last_error = NULL, last_error_at = NULL WHERE id = ?',
            (new_at, new_expires.isoformat(), account_id),
        )
    return new_at
