"""ensure_mirror: the heart of the sync logic.

For one (source_event, target_calendar, mode) triple, guarantees that
exactly one mirror exists on the target. Idempotent under crashes,
retries, syncToken full re-lists, and multi-match corruption.

See the design plan's "Identity mapping" and "Scenario 2: partial-failure
duplicate creation" sections for the rationale.
"""

import contextlib
import datetime as dt
import logging
import sqlite3
from dataclasses import dataclass
from typing import Literal

from calsync.crypto import derive_mirror_key
from calsync.gapi.client import GoogleCalendarClient
from calsync.gapi.errors import GoneError, NotFoundError
from calsync.repositories import event_links
from calsync.sync.payload import build_mirror_payload

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class CalendarRef:
    """Identifies a calendar on both DB and Google sides.

    `db_id` indexes event_links rows; `google_calendar_id` is fed to the
    Google API and to mirror_key HMAC inputs (always stable, never the
    SQLite PK). `account_label` is used for full-mode color resolution.
    """

    db_id: int
    google_calendar_id: str
    account_label: str


def _parse_event_dt(time_dict: dict) -> dt.datetime:
    """Parse Google's start/end shape into a tz-aware datetime."""
    if 'dateTime' in time_dict:
        return dt.datetime.fromisoformat(time_dict['dateTime'])
    if 'date' in time_dict:
        # All-day events should be filtered before ensure_mirror, but be defensive.
        return dt.datetime.fromisoformat(time_dict['date'] + 'T00:00:00+00:00')
    raise ValueError(f'event time has neither dateTime nor date: {time_dict!r}')


async def ensure_mirror(
    *,
    conn: sqlite3.Connection,
    client: GoogleCalendarClient,
    hmac_key: str,
    source: CalendarRef,
    target: CalendarRef,
    source_event: dict,
    mode: Literal['full', 'busy'],
    color_map: dict[str, str] | None = None,
) -> str:
    """Idempotently ensure a mirror of source_event exists on target.

    Returns the link_id. Three paths:

    1. **DB fast path**: an event_links row exists AND the Google mirror
       still exists. No mutation; return existing link_id.
    2. **Orphan adoption**: a Google event with matching mirror_key
       exists but no DB row. Adopt it; create the missing event_links
       row.
    3. **Multi-match repair**: 2+ Google events with the same
       mirror_key. Pick most-recently-updated as winner, delete losers,
       point event_links at the winner. Logs WARNING.
    4. **Fresh insert**: nothing exists anywhere. Build payload, call
       events.insert, write event_links row.
    """
    src_event_id = source_event['id']

    # 1. DB fast path
    link = event_links.find_by_source_target(
        conn,
        source_calendar_id=source.db_id,
        source_event_id=src_event_id,
        mirror_calendar_id=target.db_id,
    )
    if link:
        try:
            await client.get_event(target.google_calendar_id, link['mirror_event_id'])
            return link['link_id']  # healthy; nothing to do
        except (NotFoundError, GoneError):
            log.info(
                'mirror %s for source %s on %s went missing; will recreate',
                link['mirror_event_id'],
                src_event_id,
                target.account_label,
            )
            # fall through

    # 2. Crash-safe path: deterministic mirror_key check on Google.
    # HMAC inputs are stable Google IDs, never SQLite PKs.
    mirror_key = derive_mirror_key(
        source_google_calendar_id=source.google_calendar_id,
        source_event_id=src_event_id,
        target_google_calendar_id=target.google_calendar_id,
        mode=mode,
        hmac_key=hmac_key,
    )
    response = await client.list_events(
        target.google_calendar_id,
        private_extended_property=[f'calsync_mirror_key={mirror_key}'],
    )
    items = response.get('items', [])

    # Filter out cancelled/deleted events (showDeleted=true returns tombstones).
    items = [e for e in items if e.get('status') != 'cancelled']

    src_start = _parse_event_dt(source_event['start'])
    src_end = _parse_event_dt(source_event['end'])

    if len(items) == 1:
        google_event = items[0]
        link_id = event_links.upsert(
            conn,
            source_calendar_id=source.db_id,
            source_event_id=src_event_id,
            mirror_calendar_id=target.db_id,
            mirror_event_id=google_event['id'],
            mirror_key=mirror_key,
            mode=mode,
            source_start_at=src_start,
            source_end_at=src_end,
        )
        log.info(
            'adopted orphan mirror %s for source %s on %s',
            google_event['id'],
            src_event_id,
            target.account_label,
        )
        return link_id

    if len(items) >= 2:
        items.sort(key=lambda e: e.get('updated', ''), reverse=True)
        winner, losers = items[0], items[1:]
        log.warning(
            'multi-match for mirror_key=%s on %s: %d events; keeping %s, deleting %s',
            mirror_key,
            target.google_calendar_id,
            len(items),
            winner['id'],
            [e['id'] for e in losers],
        )
        for loser in losers:
            # Already-gone losers (404/410) are fine; the goal is "winner is the only one".
            with contextlib.suppress(NotFoundError, GoneError):
                await client.delete_event(target.google_calendar_id, loser['id'])
        link_id = event_links.upsert(
            conn,
            source_calendar_id=source.db_id,
            source_event_id=src_event_id,
            mirror_calendar_id=target.db_id,
            mirror_event_id=winner['id'],
            mirror_key=mirror_key,
            mode=mode,
            source_start_at=src_start,
            source_end_at=src_end,
        )
        return link_id

    # 4. Fresh insert
    payload = build_mirror_payload(
        source_event=source_event,
        mode=mode,
        mirror_key=mirror_key,
        source_account_label=source.account_label,
        color_map=color_map,
    )
    mirror = await client.insert_event(target.google_calendar_id, payload)
    link_id = event_links.upsert(
        conn,
        source_calendar_id=source.db_id,
        source_event_id=src_event_id,
        mirror_calendar_id=target.db_id,
        mirror_event_id=mirror['id'],
        mirror_key=mirror_key,
        mode=mode,
        source_start_at=src_start,
        source_end_at=src_end,
    )
    log.info(
        'created mirror %s for source %s on %s (mode=%s)',
        mirror['id'],
        src_event_id,
        target.account_label,
        mode,
    )
    return link_id
