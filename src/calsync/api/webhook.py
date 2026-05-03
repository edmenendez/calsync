"""POST /webhook/google.

Receives Google Calendar push notifications. Validates the tuple
(channel_id + channel_token + resource_id) against the stored row
before doing anything. Dispatches by X-Goog-Resource-State.
"""

import datetime as dt
import logging
from typing import Annotated

from fastapi import APIRouter, Depends, Header, HTTPException, Response

from calsync.config import DateWindow, NosyncConfig, Settings
from calsync.db import connect
from calsync.deps import get_settings
from calsync.repositories import watch_channels
from calsync.sync.context import build_world
from calsync.sync.delta import pull_and_process
from calsync.sync.locks import get_calendar_lock

router = APIRouter()
log = logging.getLogger(__name__)

SettingsDep = Annotated[Settings, Depends(get_settings)]


@router.post('/webhook/google')
async def google_webhook(
    settings: SettingsDep,
    x_goog_channel_id: Annotated[str | None, Header()] = None,
    x_goog_channel_token: Annotated[str | None, Header()] = None,
    x_goog_resource_id: Annotated[str | None, Header()] = None,
    x_goog_resource_state: Annotated[str | None, Header()] = None,
):
    if not all([x_goog_channel_id, x_goog_channel_token, x_goog_resource_id, x_goog_resource_state]):
        raise HTTPException(status_code=400, detail='missing required X-Goog-* headers')

    with connect(settings.db_path) as conn:
        ch = watch_channels.find_active_by_channel_id(conn, x_goog_channel_id)
        if ch is None:
            log.warning('webhook for unknown/stopped channel %s', x_goog_channel_id)
            return Response(status_code=204)

        # Tuple validation: token AND resource_id must match.
        if ch['channel_token'] != x_goog_channel_token or ch['resource_id'] != x_goog_resource_id:
            log.warning(
                'webhook tuple mismatch for channel %s (token_match=%s resource_match=%s)',
                x_goog_channel_id,
                ch['channel_token'] == x_goog_channel_token,
                ch['resource_id'] == x_goog_resource_id,
            )
            raise HTTPException(status_code=401, detail='channel tuple mismatch')

        calendar_id = ch['calendar_id']

        # Resource-state dispatch
        if x_goog_resource_state == 'sync':
            # Initial handshake; no delta to pull.
            log.info('sync handshake received for channel %s', x_goog_channel_id)
            return Response(status_code=204)

        if x_goog_resource_state == 'not_exists':
            log.info('resource %s gone; stopping channel %s', x_goog_resource_id, x_goog_channel_id)
            watch_channels.mark_stopped(conn, ch['id'])
            return Response(status_code=204)

        if x_goog_resource_state != 'exists':
            log.info('ignoring unknown resource_state=%s for channel %s', x_goog_resource_state, x_goog_channel_id)
            return Response(status_code=204)

    # 'exists' -> pull and process the delta.
    async with get_calendar_lock(calendar_id):
        with connect(settings.db_path) as conn:
            await _process_delta(conn, settings, calendar_id)

    return Response(status_code=204)


async def _process_delta(conn, settings: Settings, calendar_id: int) -> None:
    """Build world, locate source, pull and process. Run under per-calendar lock."""
    from calsync.repositories import calendars

    cal = calendars.find_by_id(conn, calendar_id)
    if cal is None:
        log.error('webhook fired for missing calendar id=%s', calendar_id)
        return

    targets_by_label, clients_by_label = await build_world(conn, settings)
    source_ref = targets_by_label.get(cal['account_label'])
    source_client = clients_by_label.get(cal['account_label'])
    if source_ref is None or source_client is None:
        log.error('failed to construct source ref/client for account %s', cal['account_label'])
        return

    # TODO: load nosync_config and date_window from a YAML config file once that
    # layer exists. Until then, defaults: no opt-out tokens, no time window
    # restriction. The plan's `current_week` window default is for production
    # safety; we keep `all` here so dev iteration isn't gated on time math.
    outcomes = await pull_and_process(
        conn=conn,
        source=source_ref,
        source_client=source_client,
        targets_by_label=targets_by_label,
        clients_by_label=clients_by_label,
        hmac_key=settings.mirror_hmac_key,
        nosync_config=NosyncConfig(),
        date_window=DateWindow(mode='all'),
        now=dt.datetime.now(dt.UTC),
        dry_run=settings.dry_run,
    )

    if outcomes:
        actions = [t.action for o in outcomes for t in o.targets]
        log.info(
            'processed %d events for %s: %s',
            len(outcomes),
            cal['account_label'],
            {a: actions.count(a) for a in set(actions)},
        )
