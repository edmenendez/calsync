"""Channel renewal job.

Runs every 6 hours; renews any active channel expiring in <24 hours.
Per the design's transactional ordering: register the new channel on
Google FIRST, then in a single DB transaction stop the old row and
insert the new one (replace_active does this), then best-effort stop
the old channel on Google.
"""

import contextlib
import datetime as dt
import logging
import secrets
import uuid

from calsync.config import Settings
from calsync.db import connect
from calsync.gapi.auth import ensure_access_token
from calsync.gapi.client import GoogleCalendarClient
from calsync.gapi.errors import GoneError, NotFoundError
from calsync.gapi.throttle import get_account_semaphore
from calsync.repositories import calendars, watch_channels

log = logging.getLogger(__name__)


async def renew_expiring_channels(settings: Settings, *, horizon_hours: int = 24) -> dict:
    """Renew every active channel whose expires_at is within `horizon_hours`.

    Returns a small summary suitable for logging.
    """
    threshold = dt.datetime.now(dt.UTC) + dt.timedelta(hours=horizon_hours)
    callback_url = f'{settings.public_url.rstrip("/")}/webhook/google'
    renewed: list[dict] = []
    failed: list[dict] = []

    with connect(settings.db_path) as conn:
        expiring = watch_channels.expiring_within(conn, threshold=threshold)
        for ch in expiring:
            cal = calendars.find_by_id(conn, ch['calendar_id'])
            if cal is None:
                continue
            try:
                access_token = await ensure_access_token(
                    conn,
                    cal['account_id'],
                    fernet_key=settings.fernet_key,
                    client_id=settings.google_client_id,
                    client_secret=settings.google_client_secret,
                )
            except Exception as e:  # noqa: BLE001
                log.exception('access token refresh failed for %s during renewal', cal['account_label'])
                failed.append({'account': cal['account_label'], 'error': type(e).__name__})
                continue

            client = GoogleCalendarClient(
                access_token,
                semaphore=get_account_semaphore(cal['account_id']),
            )
            new_id = str(uuid.uuid4())
            new_token = secrets.token_urlsafe(32)
            try:
                resp = await client.watch_events(
                    cal['google_calendar_id'],
                    channel_id=new_id,
                    channel_token=new_token,
                    callback_url=callback_url,
                )
            except Exception as e:  # noqa: BLE001
                log.exception('events.watch failed for %s during renewal', cal['account_label'])
                failed.append({'account': cal['account_label'], 'error': type(e).__name__})
                continue

            expires_at = dt.datetime.fromtimestamp(int(resp['expiration']) / 1000, dt.UTC)
            watch_channels.replace_active(
                conn,
                calendar_id=cal['cal_id'],
                channel_id=new_id,
                resource_id=resp['resourceId'],
                channel_token=new_token,
                callback_url=callback_url,
                expires_at=expires_at,
            )
            with contextlib.suppress(NotFoundError, GoneError):
                await client.stop_channel(ch['channel_id'], ch['resource_id'])
            renewed.append(
                {'account': cal['account_label'], 'new_channel_id': new_id, 'expires_at': expires_at.isoformat()}
            )

    log.info('channel renewal: renewed=%d failed=%d', len(renewed), len(failed))
    return {'renewed': renewed, 'failed': failed}
