"""Account upsert with the refresh-token-clobber guard.

OAuth callbacks may legitimately return a response without `refresh_token`
(Google sometimes omits it on re-authorizations even with prompt=consent).
The existing stored refresh token is still valid in that case, so we MUST
NOT overwrite it with NULL. Only update access_token + expiry.
"""

import datetime as dt
import sqlite3
from dataclasses import dataclass


@dataclass
class AccountAuthMismatchError(Exception):
    """Raised when the OAuth callback returns an email that conflicts with the stored one."""

    label: str
    stored_email: str
    incoming_email: str

    def __str__(self) -> str:
        return (
            f'account {self.label!r} is already authorized as {self.stored_email!r}; '
            f'refusing to switch to {self.incoming_email!r}'
        )


def upsert_oauth_credentials(
    conn: sqlite3.Connection,
    *,
    label: str,
    email: str,
    encrypted_refresh_token: bytes | None,
    access_token: str,
    access_token_expires_at: dt.datetime,
) -> int:
    """Insert or update an account with new tokens. Returns account id.

    - On INSERT: encrypted_refresh_token is required.
    - On UPDATE matching the same email: refresh_token is preserved if
      encrypted_refresh_token is None.
    - On UPDATE with a DIFFERENT email: raises AccountAuthMismatchError.
    """
    existing = conn.execute(
        'SELECT id, email, refresh_token_encrypted FROM accounts WHERE label = ?',
        (label,),
    ).fetchone()

    expires_iso = access_token_expires_at.isoformat()

    if existing is None:
        if encrypted_refresh_token is None:
            raise ValueError(
                f'cannot create account {label!r}: no refresh_token in OAuth response. '
                'This is the first authorization; a refresh_token is required.'
            )
        conn.execute(
            'INSERT INTO accounts (label, email, refresh_token_encrypted, access_token, '
            'access_token_expires_at, needs_reauth) VALUES (?, ?, ?, ?, ?, 0)',
            (label, email, encrypted_refresh_token, access_token, expires_iso),
        )
        return conn.execute('SELECT id FROM accounts WHERE label = ?', (label,)).fetchone()['id']

    if existing['email'] != email:
        raise AccountAuthMismatchError(label=label, stored_email=existing['email'], incoming_email=email)

    # Update path: preserve existing refresh_token if not provided in this response.
    if encrypted_refresh_token is None:
        conn.execute(
            'UPDATE accounts SET access_token = ?, access_token_expires_at = ?, '
            'needs_reauth = 0, last_error = NULL, last_error_at = NULL WHERE id = ?',
            (access_token, expires_iso, existing['id']),
        )
    else:
        conn.execute(
            'UPDATE accounts SET refresh_token_encrypted = ?, access_token = ?, '
            'access_token_expires_at = ?, needs_reauth = 0, last_error = NULL, '
            'last_error_at = NULL WHERE id = ?',
            (encrypted_refresh_token, access_token, expires_iso, existing['id']),
        )
    return existing['id']
