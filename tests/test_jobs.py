"""Job-function tests. Bypasses APScheduler; calls the functions directly."""

import datetime as dt

import pytest
import respx
from httpx import Response

from calsync.crypto import encrypt_token
from calsync.db import connect
from calsync.gapi.client import GOOGLE_API_BASE
from calsync.jobs.reconcile import reconcile_all
from calsync.jobs.renew_channels import renew_expiring_channels
from calsync.jobs.scheduler import create_scheduler
from calsync.repositories import watch_channels
from calsync.sync.locks import reset as reset_locks


@pytest.fixture(autouse=True)
def _reset():
    reset_locks()
    yield
    reset_locks()


def _seed(settings):
    with connect(settings.db_path) as conn:
        for label, email in [
            ('personal', 'p@x.com'),
            ('avela', 'a@x.com'),
            ('beachmedia', 'b@x.com'),
            ('novact', 'n@x.com'),
        ]:
            rt = encrypt_token('rt-' + label, settings.fernet_key)
            future = (dt.datetime.now(dt.UTC) + dt.timedelta(hours=1)).isoformat()
            conn.execute(
                'INSERT INTO accounts (label, email, refresh_token_encrypted, access_token, '
                'access_token_expires_at) VALUES (?, ?, ?, ?, ?)',
                (label, email, rt, 'at', future),
            )
        for i, email in enumerate(['p@x.com', 'a@x.com', 'b@x.com', 'n@x.com'], start=1):
            conn.execute(
                'INSERT INTO calendars (account_id, google_calendar_id, sync_token) VALUES (?, ?, ?)',
                (i, email, 'tok-' + email),
            )


# --- renew_expiring_channels ---


async def test_renew_skips_when_nothing_expiring(settings):
    _seed(settings)
    # Channel that expires 30 days out
    with connect(settings.db_path) as conn:
        watch_channels.replace_active(
            conn,
            calendar_id=1,
            channel_id='c1',
            resource_id='r1',
            channel_token='t',
            callback_url='https://x/cb',
            expires_at=dt.datetime.now(dt.UTC) + dt.timedelta(days=30),
        )
    result = await renew_expiring_channels(settings, horizon_hours=24)
    assert result['renewed'] == []
    assert result['failed'] == []


async def test_renew_replaces_channel_about_to_expire(settings):
    _seed(settings)
    soon = dt.datetime.now(dt.UTC) + dt.timedelta(hours=6)
    with connect(settings.db_path) as conn:
        watch_channels.replace_active(
            conn,
            calendar_id=1,
            channel_id='old-channel',
            resource_id='old-res',
            channel_token='t',
            callback_url='https://x/cb',
            expires_at=soon,
        )
    new_expiry_ms = int((dt.datetime.now(dt.UTC) + dt.timedelta(days=7)).timestamp() * 1000)

    with respx.mock:
        respx.post(f'{GOOGLE_API_BASE}/calendars/p@x.com/events/watch').mock(
            return_value=Response(
                200,
                json={'id': 'returned-id', 'resourceId': 'new-res', 'expiration': str(new_expiry_ms)},
            )
        )
        respx.post(f'{GOOGLE_API_BASE}/channels/stop').mock(return_value=Response(204))
        result = await renew_expiring_channels(settings, horizon_hours=24)

    assert len(result['renewed']) == 1
    assert result['renewed'][0]['account'] == 'personal'

    # DB: old row stopped, new row active. Partial unique index intact.
    with connect(settings.db_path) as conn:
        rows = conn.execute(
            'SELECT channel_id, stopped_at FROM watch_channels WHERE calendar_id = 1 ORDER BY id'
        ).fetchall()
    assert len(rows) == 2
    assert rows[0]['channel_id'] == 'old-channel'
    assert rows[0]['stopped_at'] is not None
    assert rows[1]['stopped_at'] is None


async def test_renew_handles_watch_failure_gracefully(settings):
    _seed(settings)
    soon = dt.datetime.now(dt.UTC) + dt.timedelta(hours=6)
    with connect(settings.db_path) as conn:
        watch_channels.replace_active(
            conn,
            calendar_id=1,
            channel_id='old',
            resource_id='r',
            channel_token='t',
            callback_url='https://x/',
            expires_at=soon,
        )

    with respx.mock:
        respx.post(f'{GOOGLE_API_BASE}/calendars/p@x.com/events/watch').mock(
            return_value=Response(500, json={'error': {'message': 'boom'}})
        )
        result = await renew_expiring_channels(settings, horizon_hours=24)

    assert result['renewed'] == []
    assert len(result['failed']) == 1
    assert result['failed'][0]['account'] == 'personal'

    # Old row still active (no premature stop)
    with connect(settings.db_path) as conn:
        active = watch_channels.find_active_for_calendar(conn, 1)
    assert active['channel_id'] == 'old'


# --- reconcile_all ---


async def test_reconcile_pulls_delta_for_every_calendar(settings):
    _seed(settings)
    with respx.mock:
        # Each calendar's events.list returns empty delta (no changes)
        for email in ('p@x.com', 'a@x.com', 'b@x.com', 'n@x.com'):
            respx.get(f'{GOOGLE_API_BASE}/calendars/{email}/events').mock(
                return_value=Response(200, json={'items': [], 'nextSyncToken': f'tok-{email}-new'})
            )
        result = await reconcile_all(settings)

    assert set(result.keys()) == {'personal', 'avela', 'beachmedia', 'novact'}
    assert all(v['events_seen'] == 0 and v['error'] is None for v in result.values())


async def test_reconcile_continues_when_one_account_fails(settings):
    _seed(settings)
    with respx.mock:
        # Personal's list raises 500; others return empty.
        respx.get(f'{GOOGLE_API_BASE}/calendars/p@x.com/events').mock(
            return_value=Response(500, json={'error': {'message': 'boom'}})
        )
        for email in ('a@x.com', 'b@x.com', 'n@x.com'):
            respx.get(f'{GOOGLE_API_BASE}/calendars/{email}/events').mock(
                return_value=Response(200, json={'items': [], 'nextSyncToken': f'tok-{email}-new'})
            )
        result = await reconcile_all(settings)

    assert result['personal']['error'] is not None
    assert all(result[label]['error'] is None for label in ('avela', 'beachmedia', 'novact'))


# --- scheduler factory ---


def test_create_scheduler_registers_two_jobs(settings):
    sched = create_scheduler(settings)
    job_ids = {j.id for j in sched.get_jobs()}
    assert job_ids == {'renew_channels', 'reconcile'}
    # Don't actually start; not needed for this assertion.
