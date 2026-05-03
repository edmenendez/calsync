import sqlite3
from pathlib import Path

import pytest

from calsync.db import connect, init_db


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    p = tmp_path / 'calsync_test.db'
    init_db(p)
    return p


def test_init_db_creates_tables(db_path: Path):
    with connect(db_path) as conn:
        rows = conn.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name").fetchall()
        names = {r['name'] for r in rows}
    expected = {'accounts', 'calendars', 'watch_channels', 'event_links', 'admin_log'}
    assert expected.issubset(names)


def test_init_db_is_idempotent(db_path: Path):
    init_db(db_path)
    init_db(db_path)
    with connect(db_path) as conn:
        rows = conn.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name").fetchall()
        names = [r['name'] for r in rows]
    assert names.count('accounts') == 1
    assert names.count('calendars') == 1


def test_partial_unique_index_one_active_channel_per_calendar(db_path: Path):
    """The plan's mandatory invariant: at most one row per calendar where stopped_at IS NULL."""
    with connect(db_path) as conn:
        conn.execute("INSERT INTO accounts (label, email, refresh_token_encrypted) VALUES ('a', 'a@x', x'00')")
        conn.execute("INSERT INTO calendars (account_id, google_calendar_id) VALUES (1, 'a@x')")
        conn.execute(
            'INSERT INTO watch_channels '
            '(calendar_id, channel_id, resource_id, channel_token, callback_url, expires_at) '
            "VALUES (1, 'c1', 'r1', 't1', 'https://x/', '2026-12-31')"
        )
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                'INSERT INTO watch_channels '
                '(calendar_id, channel_id, resource_id, channel_token, callback_url, expires_at) '
                "VALUES (1, 'c2', 'r2', 't2', 'https://x/', '2026-12-31')"
            )

        conn.execute("UPDATE watch_channels SET stopped_at = '2026-05-03' WHERE channel_id = 'c1'")

        conn.execute(
            'INSERT INTO watch_channels '
            '(calendar_id, channel_id, resource_id, channel_token, callback_url, expires_at) '
            "VALUES (1, 'c2', 'r2', 't2', 'https://x/', '2026-12-31')"
        )
        active = conn.execute(
            'SELECT COUNT(*) AS n FROM watch_channels WHERE calendar_id = 1 AND stopped_at IS NULL'
        ).fetchone()
        assert active['n'] == 1


def test_event_links_unique_constraint(db_path: Path):
    """One mirror per (source_event, target_calendar)."""
    with connect(db_path) as conn:
        conn.execute("INSERT INTO accounts (label, email, refresh_token_encrypted) VALUES ('a', 'a@x', x'00')")
        conn.execute("INSERT INTO calendars (account_id, google_calendar_id) VALUES (1, 'a@x')")
        conn.execute("INSERT INTO accounts (label, email, refresh_token_encrypted) VALUES ('b', 'b@y', x'00')")
        conn.execute("INSERT INTO calendars (account_id, google_calendar_id) VALUES (2, 'b@y')")
        conn.execute(
            'INSERT INTO event_links '
            '(link_id, mirror_key, source_calendar_id, source_event_id, mirror_calendar_id, '
            ' mirror_event_id, mode, source_start_at, source_end_at) '
            "VALUES ('l1', 'k1', 1, 'e1', 2, 'm1', 'busy', '2026-05-03', '2026-05-03')"
        )
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                'INSERT INTO event_links '
                '(link_id, mirror_key, source_calendar_id, source_event_id, mirror_calendar_id, '
                ' mirror_event_id, mode, source_start_at, source_end_at) '
                "VALUES ('l2', 'k1', 1, 'e1', 2, 'm2', 'busy', '2026-05-03', '2026-05-03')"
            )


def test_event_links_mode_check_constraint(db_path: Path):
    with connect(db_path) as conn:
        conn.execute("INSERT INTO accounts (label, email, refresh_token_encrypted) VALUES ('a', 'a@x', x'00')")
        conn.execute("INSERT INTO calendars (account_id, google_calendar_id) VALUES (1, 'a@x')")
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                'INSERT INTO event_links '
                '(link_id, mirror_key, source_calendar_id, source_event_id, mirror_calendar_id, '
                ' mirror_event_id, mode, source_start_at, source_end_at) '
                "VALUES ('l1', 'k1', 1, 'e1', 1, 'm1', 'wrong_mode', '2026-05-03', '2026-05-03')"
            )


def test_foreign_keys_enforced(db_path: Path):
    with connect(db_path) as conn, pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO calendars (account_id, google_calendar_id) VALUES (999, 'a@x')")
