"""watch_channels repository tests."""

import datetime as dt
import sqlite3
from pathlib import Path

import pytest

from calsync.db import connect, init_db
from calsync.repositories import watch_channels


@pytest.fixture
def db(tmp_path: Path):
    p = tmp_path / 'a.db'
    init_db(p)
    with connect(p) as conn:
        conn.execute("INSERT INTO accounts (label, email, refresh_token_encrypted) VALUES ('a', 'a@x', x'00')")
        conn.execute("INSERT INTO calendars (account_id, google_calendar_id) VALUES (1, 'a@x')")
        yield conn


def _expires() -> dt.datetime:
    return dt.datetime(2026, 5, 10, tzinfo=dt.UTC)


def test_replace_active_inserts_first_channel(db: sqlite3.Connection):
    pk = watch_channels.replace_active(
        db,
        calendar_id=1,
        channel_id='c1',
        resource_id='r1',
        channel_token='t1',
        callback_url='https://x/cb',
        expires_at=_expires(),
    )
    assert pk > 0
    found = watch_channels.find_active_for_calendar(db, 1)
    assert found is not None
    assert found['channel_id'] == 'c1'


def test_replace_active_stops_old_then_inserts_new(db: sqlite3.Connection):
    """Renewal: old row marked stopped_at, new row inserted, partial unique index respected."""
    watch_channels.replace_active(
        db,
        calendar_id=1,
        channel_id='old',
        resource_id='r1',
        channel_token='t1',
        callback_url='https://x/cb',
        expires_at=_expires(),
    )
    watch_channels.replace_active(
        db,
        calendar_id=1,
        channel_id='new',
        resource_id='r2',
        channel_token='t2',
        callback_url='https://x/cb',
        expires_at=_expires(),
    )

    rows = db.execute('SELECT channel_id, stopped_at FROM watch_channels ORDER BY id').fetchall()
    assert len(rows) == 2
    assert rows[0]['channel_id'] == 'old'
    assert rows[0]['stopped_at'] is not None  # stopped
    assert rows[1]['channel_id'] == 'new'
    assert rows[1]['stopped_at'] is None  # active

    active = watch_channels.find_active_for_calendar(db, 1)
    assert active['channel_id'] == 'new'


def test_find_active_by_channel_id_excludes_stopped(db: sqlite3.Connection):
    watch_channels.replace_active(
        db,
        calendar_id=1,
        channel_id='c1',
        resource_id='r1',
        channel_token='t',
        callback_url='https://x/',
        expires_at=_expires(),
    )
    watch_channels.replace_active(
        db,
        calendar_id=1,
        channel_id='c2',
        resource_id='r2',
        channel_token='t',
        callback_url='https://x/',
        expires_at=_expires(),
    )
    assert watch_channels.find_active_by_channel_id(db, 'c1') is None
    assert watch_channels.find_active_by_channel_id(db, 'c2') is not None


def test_mark_stopped(db: sqlite3.Connection):
    pk = watch_channels.replace_active(
        db,
        calendar_id=1,
        channel_id='c1',
        resource_id='r1',
        channel_token='t',
        callback_url='https://x/',
        expires_at=_expires(),
    )
    watch_channels.mark_stopped(db, pk)
    assert watch_channels.find_active_for_calendar(db, 1) is None


def test_expiring_within(db: sqlite3.Connection):
    """Renewal job query: returns channels expiring before threshold."""
    watch_channels.replace_active(
        db,
        calendar_id=1,
        channel_id='soon',
        resource_id='r',
        channel_token='t',
        callback_url='https://x/',
        expires_at=dt.datetime(2026, 5, 4, 6, tzinfo=dt.UTC),
    )
    threshold = dt.datetime(2026, 5, 4, 12, tzinfo=dt.UTC)
    rows = watch_channels.expiring_within(db, threshold=threshold)
    assert len(rows) == 1
    assert rows[0]['channel_id'] == 'soon'

    # Bump expiry far out -> no longer matches
    db.execute(
        "UPDATE watch_channels SET expires_at = ? WHERE channel_id = 'soon'",
        (dt.datetime(2027, 1, 1, tzinfo=dt.UTC).isoformat(),),
    )
    rows = watch_channels.expiring_within(db, threshold=threshold)
    assert rows == []
