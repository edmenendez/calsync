"""Access-token refresh + needs_reauth flag tests."""

import datetime as dt
from pathlib import Path

import pytest
import respx
from httpx import Response

from calsync.crypto import decrypt_token, encrypt_token, generate_fernet_key
from calsync.db import connect, init_db
from calsync.gapi.auth import GOOGLE_TOKEN_URL, ensure_access_token, refresh_access_token
from calsync.gapi.errors import NeedsReauthError


@pytest.fixture
def fernet_key() -> str:
    return generate_fernet_key()


@pytest.fixture
def db_with_account(tmp_path: Path, fernet_key: str):
    db = tmp_path / 'a.db'
    init_db(db)
    rt_encrypted = encrypt_token('refresh-token-stored', fernet_key)
    with connect(db) as conn:
        conn.execute(
            'INSERT INTO accounts (label, email, refresh_token_encrypted, access_token, '
            'access_token_expires_at) VALUES (?, ?, ?, ?, ?)',
            ('avela', 'ed@avela.org', rt_encrypted, 'old-at', '2026-05-03T12:00:00+00:00'),
        )
        account_id = conn.execute('SELECT id FROM accounts WHERE label = ?', ('avela',)).fetchone()['id']
    return db, account_id


async def test_refresh_access_token_happy_path():
    with respx.mock:
        respx.post(GOOGLE_TOKEN_URL).mock(
            return_value=Response(200, json={'access_token': 'new-at', 'expires_in': 3600})
        )
        result = await refresh_access_token(
            refresh_token='rt',
            client_id='cid',
            client_secret='csec',
        )
    assert result['access_token'] == 'new-at'


async def test_refresh_access_token_invalid_grant_raises_needs_reauth():
    with respx.mock:
        respx.post(GOOGLE_TOKEN_URL).mock(
            return_value=Response(
                400,
                json={'error': 'invalid_grant', 'error_description': 'Token has been expired or revoked.'},
            )
        )
        with pytest.raises(NeedsReauthError, match='expired or revoked'):
            await refresh_access_token(refresh_token='rt', client_id='cid', client_secret='csec')


async def test_ensure_returns_cached_token_when_not_expired(db_with_account, fernet_key):
    db, account_id = db_with_account
    future = (dt.datetime.now(dt.UTC) + dt.timedelta(hours=1)).isoformat()
    with connect(db) as conn:
        conn.execute(
            'UPDATE accounts SET access_token = ?, access_token_expires_at = ?',
            ('still-fresh', future),
        )

    with connect(db) as conn:
        token = await ensure_access_token(
            conn,
            account_id,
            fernet_key=fernet_key,
            client_id='cid',
            client_secret='csec',
        )
    assert token == 'still-fresh'


async def test_ensure_refreshes_when_within_buffer(db_with_account, fernet_key):
    """If token expires in <60s, refresh proactively even though it's still technically valid."""
    db, account_id = db_with_account
    soon = (dt.datetime.now(dt.UTC) + dt.timedelta(seconds=30)).isoformat()
    with connect(db) as conn:
        conn.execute(
            'UPDATE accounts SET access_token = ?, access_token_expires_at = ?',
            ('about-to-expire', soon),
        )

    with respx.mock:
        respx.post(GOOGLE_TOKEN_URL).mock(
            return_value=Response(200, json={'access_token': 'fresh-at', 'expires_in': 3600})
        )
        with connect(db) as conn:
            token = await ensure_access_token(
                conn,
                account_id,
                fernet_key=fernet_key,
                client_id='cid',
                client_secret='csec',
            )
    assert token == 'fresh-at'


async def test_ensure_refreshes_when_expired(db_with_account, fernet_key):
    db, account_id = db_with_account
    past = (dt.datetime.now(dt.UTC) - dt.timedelta(hours=1)).isoformat()
    with connect(db) as conn:
        conn.execute('UPDATE accounts SET access_token_expires_at = ?', (past,))

    with respx.mock:
        respx.post(GOOGLE_TOKEN_URL).mock(
            return_value=Response(200, json={'access_token': 'fresh-at', 'expires_in': 3600})
        )
        with connect(db) as conn:
            token = await ensure_access_token(
                conn,
                account_id,
                fernet_key=fernet_key,
                client_id='cid',
                client_secret='csec',
            )
    assert token == 'fresh-at'

    with connect(db) as conn:
        row = conn.execute('SELECT access_token FROM accounts WHERE id = ?', (account_id,)).fetchone()
    assert row['access_token'] == 'fresh-at'


async def test_ensure_invalid_grant_marks_needs_reauth(db_with_account, fernet_key):
    db, account_id = db_with_account
    past = (dt.datetime.now(dt.UTC) - dt.timedelta(hours=1)).isoformat()
    with connect(db) as conn:
        conn.execute('UPDATE accounts SET access_token_expires_at = ?', (past,))

    with respx.mock:
        respx.post(GOOGLE_TOKEN_URL).mock(
            return_value=Response(
                400,
                json={'error': 'invalid_grant', 'error_description': 'Token has been expired or revoked.'},
            )
        )
        with pytest.raises(NeedsReauthError), connect(db) as conn:
            await ensure_access_token(
                conn,
                account_id,
                fernet_key=fernet_key,
                client_id='cid',
                client_secret='csec',
            )

    with connect(db) as conn:
        row = conn.execute(
            'SELECT needs_reauth, last_error, last_error_at FROM accounts WHERE id = ?',
            (account_id,),
        ).fetchone()
    assert row['needs_reauth'] == 1
    assert row['last_error'] is not None
    assert row['last_error_at'] is not None


async def test_ensure_already_flagged_account_raises_immediately(db_with_account, fernet_key):
    """Don't even try to refresh if needs_reauth is already set."""
    db, account_id = db_with_account
    with connect(db) as conn:
        conn.execute('UPDATE accounts SET needs_reauth = 1')

    with pytest.raises(NeedsReauthError, match='re-authorize'), connect(db) as conn:
        await ensure_access_token(
            conn,
            account_id,
            fernet_key=fernet_key,
            client_id='cid',
            client_secret='csec',
        )


async def test_ensure_persists_rotated_refresh_token(db_with_account, fernet_key):
    """If Google rotates the refresh_token in the response, store the new one."""
    db, account_id = db_with_account
    past = (dt.datetime.now(dt.UTC) - dt.timedelta(hours=1)).isoformat()
    with connect(db) as conn:
        conn.execute('UPDATE accounts SET access_token_expires_at = ?', (past,))

    with respx.mock:
        respx.post(GOOGLE_TOKEN_URL).mock(
            return_value=Response(
                200,
                json={'access_token': 'new-at', 'refresh_token': 'rotated-rt', 'expires_in': 3600},
            )
        )
        with connect(db) as conn:
            await ensure_access_token(
                conn,
                account_id,
                fernet_key=fernet_key,
                client_id='cid',
                client_secret='csec',
            )

    with connect(db) as conn:
        row = conn.execute(
            'SELECT refresh_token_encrypted FROM accounts WHERE id = ?',
            (account_id,),
        ).fetchone()
    assert decrypt_token(row['refresh_token_encrypted'], fernet_key) == 'rotated-rt'


async def test_ensure_clears_last_error_on_successful_refresh(db_with_account, fernet_key):
    """After a refresh succeeds, stale last_error / last_error_at should be wiped."""
    db, account_id = db_with_account
    past = (dt.datetime.now(dt.UTC) - dt.timedelta(hours=1)).isoformat()
    with connect(db) as conn:
        conn.execute(
            "UPDATE accounts SET access_token_expires_at = ?, last_error = 'old', "
            "last_error_at = '2026-04-01' WHERE id = ?",
            (past, account_id),
        )

    with respx.mock:
        respx.post(GOOGLE_TOKEN_URL).mock(return_value=Response(200, json={'access_token': 'at', 'expires_in': 3600}))
        with connect(db) as conn:
            await ensure_access_token(
                conn,
                account_id,
                fernet_key=fernet_key,
                client_id='cid',
                client_secret='csec',
            )

    with connect(db) as conn:
        row = conn.execute('SELECT last_error, last_error_at FROM accounts WHERE id = ?', (account_id,)).fetchone()
    assert row['last_error'] is None
    assert row['last_error_at'] is None
