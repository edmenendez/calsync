"""CRUD for watch_channels.

Schema invariant: at most one row per calendar where stopped_at IS NULL
(enforced by the partial unique index). Renewal flow is in this module
to keep the transactional ordering correct.
"""

import datetime as dt
import sqlite3


def find_active_by_channel_id(conn: sqlite3.Connection, channel_id: str) -> sqlite3.Row | None:
    return conn.execute(
        'SELECT * FROM watch_channels WHERE channel_id = ? AND stopped_at IS NULL',
        (channel_id,),
    ).fetchone()


def find_active_for_calendar(conn: sqlite3.Connection, calendar_id: int) -> sqlite3.Row | None:
    return conn.execute(
        'SELECT * FROM watch_channels WHERE calendar_id = ? AND stopped_at IS NULL',
        (calendar_id,),
    ).fetchone()


def mark_stopped(conn: sqlite3.Connection, channel_pk: int, *, when: dt.datetime | None = None) -> None:
    when = when or dt.datetime.now(dt.UTC)
    conn.execute('UPDATE watch_channels SET stopped_at = ? WHERE id = ?', (when.isoformat(), channel_pk))


def replace_active(
    conn: sqlite3.Connection,
    *,
    calendar_id: int,
    channel_id: str,
    resource_id: str,
    channel_token: str,
    callback_url: str,
    expires_at: dt.datetime,
) -> int:
    """Atomic renewal: stop the existing active row, then insert the new one.

    Order matters because of the partial unique index `WHERE stopped_at
    IS NULL`. Doing both in one transaction guarantees we don't violate
    the constraint mid-flight.
    """
    now = dt.datetime.now(dt.UTC)
    conn.execute('BEGIN IMMEDIATE')
    try:
        conn.execute(
            'UPDATE watch_channels SET stopped_at = ? WHERE calendar_id = ? AND stopped_at IS NULL',
            (now.isoformat(), calendar_id),
        )
        cur = conn.execute(
            'INSERT INTO watch_channels '
            '(calendar_id, channel_id, resource_id, channel_token, callback_url, expires_at) '
            'VALUES (?, ?, ?, ?, ?, ?)',
            (calendar_id, channel_id, resource_id, channel_token, callback_url, expires_at.isoformat()),
        )
        conn.execute('COMMIT')
        new_id = cur.lastrowid
        assert new_id is not None
        return new_id
    except Exception:
        conn.execute('ROLLBACK')
        raise


def expiring_within(conn: sqlite3.Connection, *, threshold: dt.datetime) -> list[sqlite3.Row]:
    """Active channels whose expires_at falls before threshold.

    Renewal job calls this hourly with threshold = now + 24h.
    """
    return conn.execute(
        'SELECT * FROM watch_channels WHERE stopped_at IS NULL AND expires_at < ? ORDER BY expires_at',
        (threshold.isoformat(),),
    ).fetchall()
