"""CRUD for the event_links table.

Each row records one (source_event, target_calendar) -> mirror_event
mapping. Lookups support the ensure_mirror flow (DB fast path), the
echo-loop defense (lookup by mirror_event_id), and cleanup operations.
"""

import datetime as dt
import sqlite3
import uuid


def find_by_source_target(
    conn: sqlite3.Connection,
    *,
    source_calendar_id: int,
    source_event_id: str,
    mirror_calendar_id: int,
) -> sqlite3.Row | None:
    """Fast path lookup for ensure_mirror's DB-side check."""
    return conn.execute(
        'SELECT * FROM event_links WHERE source_calendar_id = ? AND source_event_id = ? AND mirror_calendar_id = ?',
        (source_calendar_id, source_event_id, mirror_calendar_id),
    ).fetchone()


def find_by_mirror_event(
    conn: sqlite3.Connection, *, mirror_calendar_id: int, mirror_event_id: str
) -> sqlite3.Row | None:
    """Echo-loop defense layer 2: skip events whose ID is in event_links."""
    return conn.execute(
        'SELECT * FROM event_links WHERE mirror_calendar_id = ? AND mirror_event_id = ?',
        (mirror_calendar_id, mirror_event_id),
    ).fetchone()


def find_by_link_id(conn: sqlite3.Connection, link_id: str) -> sqlite3.Row | None:
    return conn.execute('SELECT * FROM event_links WHERE link_id = ?', (link_id,)).fetchone()


def find_all_for_source(
    conn: sqlite3.Connection, *, source_calendar_id: int, source_event_id: str
) -> list[sqlite3.Row]:
    """All mirrors of one source event across all target calendars.

    Used when a source event is deleted/updated and we need to fan-out
    the change to every mirror.
    """
    return conn.execute(
        'SELECT * FROM event_links WHERE source_calendar_id = ? AND source_event_id = ?',
        (source_calendar_id, source_event_id),
    ).fetchall()


def upsert(
    conn: sqlite3.Connection,
    *,
    source_calendar_id: int,
    source_event_id: str,
    mirror_calendar_id: int,
    mirror_event_id: str,
    mirror_key: str,
    mode: str,
    source_start_at: dt.datetime,
    source_end_at: dt.datetime,
) -> str:
    """Insert or update an event_link row. Returns the link_id.

    Update path is keyed on the UNIQUE(source_calendar_id, source_event_id,
    mirror_calendar_id) constraint. Preserves the existing link_id if the
    row already exists - useful so external observers (logs, traces) see
    a stable identifier per logical mirror.
    """
    existing = find_by_source_target(
        conn,
        source_calendar_id=source_calendar_id,
        source_event_id=source_event_id,
        mirror_calendar_id=mirror_calendar_id,
    )
    if existing is None:
        link_id = str(uuid.uuid4())
        conn.execute(
            'INSERT INTO event_links '
            '(link_id, mirror_key, source_calendar_id, source_event_id, '
            ' mirror_calendar_id, mirror_event_id, mode, source_start_at, source_end_at) '
            'VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)',
            (
                link_id,
                mirror_key,
                source_calendar_id,
                source_event_id,
                mirror_calendar_id,
                mirror_event_id,
                mode,
                source_start_at.isoformat(),
                source_end_at.isoformat(),
            ),
        )
        return link_id

    conn.execute(
        'UPDATE event_links SET '
        '  mirror_key = ?, mirror_event_id = ?, mode = ?, '
        '  source_start_at = ?, source_end_at = ? '
        'WHERE id = ?',
        (
            mirror_key,
            mirror_event_id,
            mode,
            source_start_at.isoformat(),
            source_end_at.isoformat(),
            existing['id'],
        ),
    )
    return existing['link_id']


def delete_by_id(conn: sqlite3.Connection, link_id: str) -> None:
    conn.execute('DELETE FROM event_links WHERE link_id = ?', (link_id,))


def delete_all_for_source(conn: sqlite3.Connection, *, source_calendar_id: int, source_event_id: str) -> int:
    """Used when a source event is deleted; returns count of removed rows."""
    cur = conn.execute(
        'DELETE FROM event_links WHERE source_calendar_id = ? AND source_event_id = ?',
        (source_calendar_id, source_event_id),
    )
    return cur.rowcount
