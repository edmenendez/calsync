"""Pull a calendar's syncToken delta and dispatch each event to the processor.

Called from the webhook handler (after tuple validation) and from the
hourly backstop reconciliation.

Pagination contract: persist the new `nextSyncToken` ONLY after fully
paging through the response. If anything fails mid-pagination, the OLD
token stays in the DB and the next run replays the entire delta - safe
because every operation downstream is idempotent.

410 Gone handling: if Google says the syncToken is too old, clear it
and do a full window-bounded re-list. ensure_mirror's HMAC mirror_key
check makes the re-list duplicate-safe.
"""

import datetime as dt
import logging
import sqlite3

from calsync.config import DateWindow, NosyncConfig
from calsync.gapi.client import GoogleCalendarClient
from calsync.gapi.errors import GoneError
from calsync.repositories import calendars
from calsync.sync.filters import _resolve_window_bounds
from calsync.sync.matrix import Mode
from calsync.sync.mirror import CalendarRef
from calsync.sync.processor import ProcessOutcome, process_event

log = logging.getLogger(__name__)


async def pull_and_process(
    *,
    conn: sqlite3.Connection,
    source: CalendarRef,
    source_client: GoogleCalendarClient,
    targets_by_label: dict[str, CalendarRef],
    clients_by_label: dict[str, GoogleCalendarClient],
    hmac_key: str,
    nosync_config: NosyncConfig,
    date_window: DateWindow,
    now: dt.datetime,
    matrix: list[tuple[str, str, Mode]] | None = None,
    color_map: dict[str, str] | None = None,
    dry_run: bool = False,
) -> list[ProcessOutcome]:
    """Pull source's events delta + dispatch each to the processor.

    Reads `calendars.sync_token` from DB. If absent, does a window-bounded
    full list. If 410, clears the token and retries with full list.

    Saves the new sync_token only after every page processes cleanly.
    """
    cal_row = calendars.find_by_id(conn, source.db_id)
    if cal_row is None:
        raise ValueError(f'calendar {source.db_id} not found')
    sync_token = cal_row['sync_token']

    outcomes: list[ProcessOutcome] = []
    new_sync_token: str | None = None

    page_token: str | None = None
    used_full_list = sync_token is None
    full_list_kwargs = _full_list_kwargs(date_window, now) if used_full_list else {}

    while True:
        try:
            if used_full_list:
                resp = await source_client.list_events(
                    source.google_calendar_id,
                    page_token=page_token,
                    **full_list_kwargs,
                )
            else:
                resp = await source_client.list_events(
                    source.google_calendar_id,
                    sync_token=sync_token,
                    page_token=page_token,
                )
        except GoneError:
            log.warning('syncToken expired for %s; falling back to full re-list', source.account_label)
            calendars.clear_sync_token(conn, source.db_id)
            sync_token = None
            page_token = None
            used_full_list = True
            full_list_kwargs = _full_list_kwargs(date_window, now)
            continue

        for event in resp.get('items', []):
            outcome = await process_event(
                conn=conn,
                source=source,
                source_event=event,
                targets_by_label=targets_by_label,
                clients_by_label=clients_by_label,
                hmac_key=hmac_key,
                nosync_config=nosync_config,
                date_window=date_window,
                now=now,
                matrix=matrix,
                color_map=color_map,
                dry_run=dry_run,
            )
            outcomes.append(outcome)

        page_token = resp.get('nextPageToken')
        if not page_token:
            new_sync_token = resp.get('nextSyncToken')
            break

    if new_sync_token and not dry_run:
        calendars.update_sync_token(
            conn,
            calendar_id=source.db_id,
            sync_token=new_sync_token,
            last_sync_at=now.isoformat(),
        )

    return outcomes


def _full_list_kwargs(date_window: DateWindow, now: dt.datetime) -> dict:
    """Build time_min/time_max for a full list bounded by the date window.

    For mode='all', returns empty dict (no time filter). For other modes,
    uses the same bounds as is_within_window to keep "what's pulled"
    consistent with "what's eligible to mirror".
    """
    lo, hi = _resolve_window_bounds(date_window, now)
    kwargs: dict = {}
    if lo is not None:
        kwargs['time_min'] = lo.isoformat()
    if hi is not None:
        kwargs['time_max'] = hi.isoformat()
    return kwargs
