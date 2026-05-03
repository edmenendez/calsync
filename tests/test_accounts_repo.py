"""Tests for the refresh-token-clobber guard and email-mismatch protection."""

import datetime as dt
from pathlib import Path

import pytest

from calsync.db import connect, init_db
from calsync.repositories.accounts import AccountAuthMismatchError, upsert_oauth_credentials


@pytest.fixture
def conn(tmp_path: Path):
    db = tmp_path / 'a.db'
    init_db(db)
    with connect(db) as c:
        yield c


def test_first_authorization_inserts(conn):
    aid = upsert_oauth_credentials(
        conn,
        label='avela',
        email='ed@avela.org',
        encrypted_refresh_token=b'enc-rt-1',
        access_token='at-1',
        access_token_expires_at=dt.datetime(2026, 5, 3, 12, 0, 0),
    )
    assert aid > 0
    row = conn.execute('SELECT * FROM accounts WHERE label = ?', ('avela',)).fetchone()
    assert row['email'] == 'ed@avela.org'
    assert row['refresh_token_encrypted'] == b'enc-rt-1'
    assert row['access_token'] == 'at-1'
    assert row['needs_reauth'] == 0


def test_first_authorization_without_refresh_token_fails(conn):
    with pytest.raises(ValueError, match='refresh_token is required'):
        upsert_oauth_credentials(
            conn,
            label='avela',
            email='ed@avela.org',
            encrypted_refresh_token=None,
            access_token='at-1',
            access_token_expires_at=dt.datetime(2026, 5, 3, 12, 0, 0),
        )


def test_reauth_with_refresh_token_replaces_it(conn):
    upsert_oauth_credentials(
        conn,
        label='avela',
        email='ed@avela.org',
        encrypted_refresh_token=b'old-rt',
        access_token='old-at',
        access_token_expires_at=dt.datetime(2026, 5, 3, 12, 0, 0),
    )
    upsert_oauth_credentials(
        conn,
        label='avela',
        email='ed@avela.org',
        encrypted_refresh_token=b'new-rt',
        access_token='new-at',
        access_token_expires_at=dt.datetime(2026, 5, 3, 13, 0, 0),
    )
    row = conn.execute('SELECT * FROM accounts WHERE label = ?', ('avela',)).fetchone()
    assert row['refresh_token_encrypted'] == b'new-rt'
    assert row['access_token'] == 'new-at'


def test_reauth_without_refresh_token_preserves_existing(conn):
    """The clobber-guard: if Google returns no refresh_token, keep the old one."""
    upsert_oauth_credentials(
        conn,
        label='avela',
        email='ed@avela.org',
        encrypted_refresh_token=b'preserved-rt',
        access_token='old-at',
        access_token_expires_at=dt.datetime(2026, 5, 3, 12, 0, 0),
    )
    upsert_oauth_credentials(
        conn,
        label='avela',
        email='ed@avela.org',
        encrypted_refresh_token=None,
        access_token='new-at',
        access_token_expires_at=dt.datetime(2026, 5, 3, 13, 0, 0),
    )
    row = conn.execute('SELECT * FROM accounts WHERE label = ?', ('avela',)).fetchone()
    assert row['refresh_token_encrypted'] == b'preserved-rt'
    assert row['access_token'] == 'new-at'


def test_reauth_with_different_email_rejected(conn):
    upsert_oauth_credentials(
        conn,
        label='avela',
        email='ed@avela.org',
        encrypted_refresh_token=b'rt-1',
        access_token='at',
        access_token_expires_at=dt.datetime(2026, 5, 3, 12, 0, 0),
    )
    with pytest.raises(AccountAuthMismatchError) as ei:
        upsert_oauth_credentials(
            conn,
            label='avela',
            email='someone-else@avela.org',
            encrypted_refresh_token=b'rt-2',
            access_token='at',
            access_token_expires_at=dt.datetime(2026, 5, 3, 12, 0, 0),
        )
    assert ei.value.stored_email == 'ed@avela.org'
    assert ei.value.incoming_email == 'someone-else@avela.org'


def test_reauth_clears_needs_reauth_and_last_error(conn):
    upsert_oauth_credentials(
        conn,
        label='avela',
        email='ed@avela.org',
        encrypted_refresh_token=b'rt',
        access_token='at',
        access_token_expires_at=dt.datetime(2026, 5, 3, 12, 0, 0),
    )
    conn.execute(
        "UPDATE accounts SET needs_reauth = 1, last_error = 'invalid_grant', "
        "last_error_at = '2026-05-03' WHERE label = 'avela'"
    )
    upsert_oauth_credentials(
        conn,
        label='avela',
        email='ed@avela.org',
        encrypted_refresh_token=None,
        access_token='at-2',
        access_token_expires_at=dt.datetime(2026, 5, 3, 13, 0, 0),
    )
    row = conn.execute('SELECT * FROM accounts WHERE label = ?', ('avela',)).fetchone()
    assert row['needs_reauth'] == 0
    assert row['last_error'] is None
    assert row['last_error_at'] is None
