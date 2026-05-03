"""Build the all-accounts context the processor needs.

Refreshes access tokens, constructs a GoogleCalendarClient per account
(sharing per-account semaphores), and produces the (targets_by_label,
clients_by_label) pair the processor expects.

This module is the integration point between the FastAPI request layer
and the sync layer.
"""

import sqlite3

from calsync.config import Settings
from calsync.gapi.auth import ensure_access_token
from calsync.gapi.client import GoogleCalendarClient
from calsync.gapi.throttle import get_account_semaphore
from calsync.repositories import calendars
from calsync.sync.mirror import CalendarRef


async def build_world(
    conn: sqlite3.Connection,
    settings: Settings,
) -> tuple[dict[str, CalendarRef], dict[str, GoogleCalendarClient]]:
    """Returns (targets_by_label, clients_by_label) for every account.

    Each client carries the per-account semaphore so concurrent operations
    against the same account stay within the configured concurrency cap.
    """
    targets: dict[str, CalendarRef] = {}
    clients: dict[str, GoogleCalendarClient] = {}

    for row in calendars.all_calendars(conn):
        access_token = await ensure_access_token(
            conn,
            row['account_id'],
            fernet_key=settings.fernet_key,
            client_id=settings.google_client_id,
            client_secret=settings.google_client_secret,
        )
        targets[row['account_label']] = CalendarRef(
            db_id=row['cal_id'],
            google_calendar_id=row['google_calendar_id'],
            account_label=row['account_label'],
        )
        clients[row['account_label']] = GoogleCalendarClient(
            access_token=access_token,
            semaphore=get_account_semaphore(row['account_id']),
        )

    return targets, clients
