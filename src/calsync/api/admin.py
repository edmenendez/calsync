"""Admin endpoints. Localhost-only in prod (via Caddy); X-Admin-Token always.

Canonical list (must stay in sync with the design plan):
- GET  /admin/status
- GET  /admin/quota          (TODO: implementation pending API counter)
- GET  /admin/dryrun-week
- POST /admin/setup-watches
- POST /admin/cleanup/event
- POST /admin/cleanup/calendar
- POST /admin/cleanup/all
- POST /admin/uninstall

Every endpoint:
- Requires header X-Admin-Token (constant-time compared)
- Writes a row to admin_log on every call
"""

import contextlib
import datetime as dt
import hashlib
import hmac as hmac_mod
import logging
import secrets
import sqlite3
import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from pydantic import BaseModel

from calsync.config import DateWindow, NosyncConfig, Settings
from calsync.db import connect
from calsync.deps import get_settings
from calsync.gapi.client import GoogleCalendarClient
from calsync.gapi.errors import GoneError, NotFoundError
from calsync.repositories import calendars, event_links, watch_channels
from calsync.sync.context import build_world
from calsync.sync.delta import pull_and_process

router = APIRouter(prefix='/admin')
log = logging.getLogger(__name__)

SettingsDep = Annotated[Settings, Depends(get_settings)]


def _verify_admin_token(request: Request, settings: Settings, header_value: str | None) -> None:
    if header_value is None:
        raise HTTPException(status_code=401, detail='X-Admin-Token required')
    if not hmac_mod.compare_digest(header_value, settings.admin_token):
        raise HTTPException(status_code=401, detail='X-Admin-Token invalid')


async def admin_auth(
    request: Request,
    settings: SettingsDep,
    x_admin_token: Annotated[str | None, Header()] = None,
) -> Settings:
    _verify_admin_token(request, settings, x_admin_token)
    return settings


AdminAuth = Annotated[Settings, Depends(admin_auth)]


def _audit(conn: sqlite3.Connection, request: Request, payload: dict | None, status: int, notes: str = '') -> None:
    body_hash = ''
    if payload:
        body_hash = hashlib.sha256(repr(sorted(payload.items())).encode()).hexdigest()
    conn.execute(
        'INSERT INTO admin_log (endpoint, method, payload_sha256, source_ip, result_code, notes) '
        'VALUES (?, ?, ?, ?, ?, ?)',
        (
            str(request.url.path),
            request.method,
            body_hash,
            request.client.host if request.client else '',
            status,
            notes,
        ),
    )


# ---- request bodies ----


class CleanupEventBody(BaseModel):
    source_account: str
    source_event_id: str


class CleanupCalendarBody(BaseModel):
    account: str


# ---- GET /admin/status ----


@router.get('/status')
async def admin_status(request: Request, settings: AdminAuth):
    with connect(settings.db_path) as conn:
        accounts_rows = conn.execute(
            'SELECT a.id, a.label, a.email, a.needs_reauth, a.paused, a.last_error, a.last_error_at, '
            '       c.id AS cal_id, c.google_calendar_id, c.sync_token IS NOT NULL AS has_sync_token, '
            '       c.last_sync_at '
            'FROM accounts a LEFT JOIN calendars c ON c.account_id = a.id '
            'ORDER BY a.id'
        ).fetchall()
        channels_rows = conn.execute(
            'SELECT calendar_id, channel_id, expires_at FROM watch_channels WHERE stopped_at IS NULL'
        ).fetchall()
        link_count = conn.execute('SELECT COUNT(*) AS n FROM event_links').fetchone()['n']
        _audit(conn, request, None, 200)

    channels_by_cal = {c['calendar_id']: dict(c) for c in channels_rows}
    return {
        'accounts': [
            {
                'label': r['label'],
                'email': r['email'],
                'needs_reauth': bool(r['needs_reauth']),
                'paused': bool(r['paused']),
                'last_error': r['last_error'],
                'last_error_at': r['last_error_at'],
                'has_sync_token': bool(r['has_sync_token']) if r['has_sync_token'] is not None else False,
                'last_sync_at': r['last_sync_at'],
                'active_channel': channels_by_cal.get(r['cal_id']),
            }
            for r in accounts_rows
        ],
        'event_links_count': link_count,
    }


# ---- GET /admin/dryrun-week ----


@router.get('/dryrun-week')
async def admin_dryrun_week(request: Request, settings: AdminAuth):
    """Run pull_and_process in dry_run mode for every account's current week."""
    now = dt.datetime.now(dt.UTC)
    with connect(settings.db_path) as conn:
        targets, clients = await build_world(conn, settings)
        results = {}
        for label, source_ref in targets.items():
            outcomes = await pull_and_process(
                conn=conn,
                source=source_ref,
                source_client=clients[label],
                targets_by_label=targets,
                clients_by_label=clients,
                hmac_key=settings.mirror_hmac_key,
                nosync_config=NosyncConfig(),
                date_window=DateWindow(mode='current_week'),
                now=now,
                dry_run=True,
            )
            results[label] = [
                {
                    'source_event_id': o.source_event_id,
                    'targets': [
                        {'target': t.target_label, 'mode': t.mode, 'action': t.action, 'reason': t.reason}
                        for t in o.targets
                    ],
                }
                for o in outcomes
            ]
        _audit(conn, request, None, 200)
    return results


# ---- POST /admin/setup-watches ----


@router.post('/setup-watches')
async def admin_setup_watches(request: Request, settings: AdminAuth):
    """Register a watch channel for every configured calendar.

    Idempotent: if a calendar already has an active channel, it gets
    replaced via watch_channels.replace_active (old stopped + new
    inserted in one transaction). Always best-effort stops the old
    channel on Google after the DB swap.
    """
    callback_url = f'{settings.public_url.rstrip("/")}/webhook/google'
    summary = []
    with connect(settings.db_path) as conn:
        targets, clients = await build_world(conn, settings)
        for label, ref in targets.items():
            client = clients[label]
            channel_id = str(uuid.uuid4())
            channel_token = secrets.token_urlsafe(32)
            try:
                resp = await client.watch_events(
                    ref.google_calendar_id,
                    channel_id=channel_id,
                    channel_token=channel_token,
                    callback_url=callback_url,
                )
            except Exception as e:  # noqa: BLE001
                log.exception('watch registration failed for %s: %s', label, e)
                summary.append({'account': label, 'status': 'failed', 'error': str(e)})
                continue
            expires_at_ms = int(resp['expiration'])
            expires_at = dt.datetime.fromtimestamp(expires_at_ms / 1000, dt.UTC)

            # Capture the OLD active channel BEFORE replace_active so we can stop it on Google after.
            old = watch_channels.find_active_for_calendar(conn, ref.db_id)
            watch_channels.replace_active(
                conn,
                calendar_id=ref.db_id,
                channel_id=channel_id,
                resource_id=resp['resourceId'],
                channel_token=channel_token,
                callback_url=callback_url,
                expires_at=expires_at,
            )
            if old is not None:
                with contextlib.suppress(NotFoundError, GoneError):
                    await client.stop_channel(old['channel_id'], old['resource_id'])
            summary.append(
                {'account': label, 'status': 'ok', 'channel_id': channel_id, 'expires_at': expires_at.isoformat()}
            )
        _audit(conn, request, None, 200)
    return {'channels': summary}


# ---- POST /admin/cleanup/event ----


@router.post('/cleanup/event')
async def admin_cleanup_event(request: Request, body: CleanupEventBody, settings: AdminAuth):
    with connect(settings.db_path) as conn:
        targets, clients = await build_world(conn, settings)
        source_ref = targets.get(body.source_account)
        if source_ref is None:
            _audit(conn, request, body.model_dump(), 404, 'unknown_account')
            raise HTTPException(status_code=404, detail=f'unknown account {body.source_account!r}')

        rows = event_links.find_all_for_source(
            conn,
            source_calendar_id=source_ref.db_id,
            source_event_id=body.source_event_id,
        )
        deleted = 0
        for row in rows:
            target = next((t for t in targets.values() if t.db_id == row['mirror_calendar_id']), None)
            if target is None:
                continue
            client = clients[target.account_label]
            with contextlib.suppress(NotFoundError, GoneError):
                await client.delete_event(target.google_calendar_id, row['mirror_event_id'])
            event_links.delete_by_id(conn, row['link_id'])
            deleted += 1
        _audit(conn, request, body.model_dump(), 200, f'deleted={deleted}')
    return {'source_account': body.source_account, 'source_event_id': body.source_event_id, 'deleted': deleted}


# ---- POST /admin/cleanup/calendar ----


@router.post('/cleanup/calendar')
async def admin_cleanup_calendar(request: Request, body: CleanupCalendarBody, settings: AdminAuth):
    """Self-contained wipe-and-resume: pause, stop watch, delete every calsync
    event on the calendar, drop event_links, clear sync_token, re-arm.
    """
    with connect(settings.db_path) as conn:
        cal = calendars.find_by_account_label(conn, body.account)
        if cal is None:
            _audit(conn, request, body.model_dump(), 404, 'unknown_account')
            raise HTTPException(status_code=404, detail=f'unknown account {body.account!r}')

        targets, clients = await build_world(conn, settings)
        client = clients[body.account]
        ref = targets[body.account]

        # 1. pause + 2. stop watch
        conn.execute('UPDATE accounts SET paused = 1 WHERE id = ?', (cal['account_id'],))
        active = watch_channels.find_active_for_calendar(conn, ref.db_id)
        if active:
            with contextlib.suppress(NotFoundError, GoneError):
                await client.stop_channel(active['channel_id'], active['resource_id'])
            watch_channels.mark_stopped(conn, active['id'])

        # 3. delete every calsync-tagged event on this calendar
        deleted = await _delete_all_calsync_events(client, ref)

        # 4. drop event_links rows where this calendar is the mirror target
        conn.execute('DELETE FROM event_links WHERE mirror_calendar_id = ?', (ref.db_id,))

        # 5. clear sync_token (next pull is a fresh window-bounded list)
        calendars.clear_sync_token(conn, ref.db_id)

        # 6. unpause
        conn.execute('UPDATE accounts SET paused = 0 WHERE id = ?', (cal['account_id'],))
        _audit(conn, request, body.model_dump(), 200, f'deleted={deleted}')

    return {'account': body.account, 'deleted_events': deleted, 'state': 'paused-then-resumed'}


# ---- POST /admin/cleanup/all ----


@router.post('/cleanup/all')
async def admin_cleanup_all(request: Request, settings: AdminAuth):
    with connect(settings.db_path) as conn:
        targets, clients = await build_world(conn, settings)
        per_account = {}
        for label, ref in targets.items():
            client = clients[label]
            deleted = await _delete_all_calsync_events(client, ref)
            per_account[label] = deleted

        # Drop ALL event_links and clear sync tokens; channels stay so events resume on next webhook.
        conn.execute('DELETE FROM event_links')
        for ref in targets.values():
            calendars.clear_sync_token(conn, ref.db_id)
        _audit(conn, request, None, 200, f'per_account={per_account}')

    return {'deleted_events_per_account': per_account}


# ---- POST /admin/uninstall ----


@router.post('/uninstall')
async def admin_uninstall(request: Request, settings: AdminAuth):
    with connect(settings.db_path) as conn:
        targets, clients = await build_world(conn, settings)
        per_account = {}

        for label, ref in targets.items():
            client = clients[label]
            # Stop active watch channel
            active = watch_channels.find_active_for_calendar(conn, ref.db_id)
            if active:
                with contextlib.suppress(NotFoundError, GoneError):
                    await client.stop_channel(active['channel_id'], active['resource_id'])
                watch_channels.mark_stopped(conn, active['id'])
            # Delete all calsync events
            per_account[label] = await _delete_all_calsync_events(client, ref)

        conn.execute('DELETE FROM event_links')
        for ref in targets.values():
            calendars.clear_sync_token(conn, ref.db_id)
        _audit(conn, request, None, 200, f'per_account={per_account}')

    return {'deleted_events_per_account': per_account, 'channels_stopped': True}


# ---- helpers ----


async def _delete_all_calsync_events(client: GoogleCalendarClient, ref) -> int:
    """Page through every calsync-tagged event on `ref` calendar and delete."""
    deleted = 0
    page_token: str | None = None
    while True:
        resp = await client.list_events(
            ref.google_calendar_id,
            private_extended_property=['calsync_origin=calsync'],
            page_token=page_token,
            show_deleted=False,  # tombstones already cleaned up
            max_results=250,
        )
        for ev in resp.get('items', []):
            try:
                await client.delete_event(ref.google_calendar_id, ev['id'])
                deleted += 1
            except (NotFoundError, GoneError):
                pass
        page_token = resp.get('nextPageToken')
        if not page_token:
            break
    return deleted
