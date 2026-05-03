"""Calendar/account joins.

Most reads need (calendar.id, calendar.google_calendar_id, account.label,
account.id) together; this module provides those bundled lookups.
"""

import sqlite3


def find_by_id(conn: sqlite3.Connection, calendar_id: int) -> sqlite3.Row | None:
    return conn.execute(
        'SELECT c.id AS cal_id, c.google_calendar_id, c.sync_token, c.last_sync_at, '
        '       a.id AS account_id, a.label AS account_label, a.email '
        'FROM calendars c JOIN accounts a ON a.id = c.account_id '
        'WHERE c.id = ?',
        (calendar_id,),
    ).fetchone()


def find_by_account_label(conn: sqlite3.Connection, label: str) -> sqlite3.Row | None:
    return conn.execute(
        'SELECT c.id AS cal_id, c.google_calendar_id, c.sync_token, c.last_sync_at, '
        '       a.id AS account_id, a.label AS account_label, a.email '
        'FROM calendars c JOIN accounts a ON a.id = c.account_id '
        'WHERE a.label = ?',
        (label,),
    ).fetchone()


def all_calendars(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        'SELECT c.id AS cal_id, c.google_calendar_id, c.sync_token, c.last_sync_at, '
        '       a.id AS account_id, a.label AS account_label, a.email '
        'FROM calendars c JOIN accounts a ON a.id = c.account_id '
        'ORDER BY a.id'
    ).fetchall()


def update_sync_token(conn: sqlite3.Connection, *, calendar_id: int, sync_token: str, last_sync_at: str) -> None:
    conn.execute(
        'UPDATE calendars SET sync_token = ?, last_sync_at = ? WHERE id = ?',
        (sync_token, last_sync_at, calendar_id),
    )


def clear_sync_token(conn: sqlite3.Connection, calendar_id: int) -> None:
    conn.execute('UPDATE calendars SET sync_token = NULL WHERE id = ?', (calendar_id,))
