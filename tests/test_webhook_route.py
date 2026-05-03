"""Webhook route tests: tuple validation, resource_state dispatch, delta processing."""

import datetime as dt

import pytest
import respx
from httpx import Response

from calsync.crypto import encrypt_token
from calsync.db import connect
from calsync.gapi.client import GOOGLE_API_BASE
from calsync.repositories import event_links, watch_channels
from calsync.sync.locks import reset as reset_locks


@pytest.fixture(autouse=True)
def _reset_state():
    reset_locks()
    yield
    reset_locks()


def _seed_accounts_and_channel(settings, channel_id='ch-1', resource_id='res-1', token='secret-tok'):
    """Insert 4 accounts + 4 calendars + an active watch channel on personal."""
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
                (label, email, rt, 'at-' + label, future),
            )
        for i, email in enumerate(['p@x.com', 'a@x.com', 'b@x.com', 'n@x.com'], start=1):
            conn.execute(
                'INSERT INTO calendars (account_id, google_calendar_id, sync_token) VALUES (?, ?, ?)',
                (i, email, 'tok-' + email),
            )
        # Watch channel on the personal calendar (cal_id=1).
        watch_channels.replace_active(
            conn,
            calendar_id=1,
            channel_id=channel_id,
            resource_id=resource_id,
            channel_token=token,
            callback_url='https://x/cb',
            expires_at=dt.datetime(2026, 5, 10, tzinfo=dt.UTC),
        )


# --- header / tuple validation ---


def test_webhook_missing_headers_returns_400(app_client, settings):
    r = app_client.post('/webhook/google')
    assert r.status_code == 400


def test_webhook_unknown_channel_acks_with_204(app_client, settings):
    r = app_client.post(
        '/webhook/google',
        headers={
            'X-Goog-Channel-Id': 'unknown',
            'X-Goog-Channel-Token': 't',
            'X-Goog-Resource-Id': 'r',
            'X-Goog-Resource-State': 'exists',
        },
    )
    assert r.status_code == 204


def test_webhook_token_mismatch_returns_401(app_client, settings):
    _seed_accounts_and_channel(settings, channel_id='ch-1', token='right-token')
    r = app_client.post(
        '/webhook/google',
        headers={
            'X-Goog-Channel-Id': 'ch-1',
            'X-Goog-Channel-Token': 'wrong-token',
            'X-Goog-Resource-Id': 'res-1',
            'X-Goog-Resource-State': 'exists',
        },
    )
    assert r.status_code == 401


def test_webhook_resource_id_mismatch_returns_401(app_client, settings):
    _seed_accounts_and_channel(settings, resource_id='res-1')
    r = app_client.post(
        '/webhook/google',
        headers={
            'X-Goog-Channel-Id': 'ch-1',
            'X-Goog-Channel-Token': 'secret-tok',
            'X-Goog-Resource-Id': 'wrong-resource',
            'X-Goog-Resource-State': 'exists',
        },
    )
    assert r.status_code == 401


# --- resource state dispatch ---


def test_webhook_sync_state_acks_without_processing(app_client, settings):
    _seed_accounts_and_channel(settings)
    with respx.mock:
        # If processing happened, this would be hit - mock NOT to raise just to verify
        respx.get(f'{GOOGLE_API_BASE}/calendars/p@x.com/events').mock(
            return_value=Response(500, json={'error': 'should not be called'})
        )
        r = app_client.post(
            '/webhook/google',
            headers={
                'X-Goog-Channel-Id': 'ch-1',
                'X-Goog-Channel-Token': 'secret-tok',
                'X-Goog-Resource-Id': 'res-1',
                'X-Goog-Resource-State': 'sync',
            },
        )
    assert r.status_code == 204


def test_webhook_not_exists_state_marks_channel_stopped(app_client, settings):
    _seed_accounts_and_channel(settings)
    r = app_client.post(
        '/webhook/google',
        headers={
            'X-Goog-Channel-Id': 'ch-1',
            'X-Goog-Channel-Token': 'secret-tok',
            'X-Goog-Resource-Id': 'res-1',
            'X-Goog-Resource-State': 'not_exists',
        },
    )
    assert r.status_code == 204
    with connect(settings.db_path) as conn:
        active = watch_channels.find_active_for_calendar(conn, 1)
        assert active is None


# --- exists -> full processing ---


def test_webhook_exists_pulls_delta_and_dispatches(app_client, settings):
    _seed_accounts_and_channel(settings)
    src_event = {
        'id': 'src-event-1',
        'summary': 'New meeting',
        'description': '',
        'start': {'dateTime': '2026-05-04T10:00:00+00:00'},
        'end': {'dateTime': '2026-05-04T11:00:00+00:00'},
    }

    with respx.mock:
        # Source delta: one new event
        respx.get(f'{GOOGLE_API_BASE}/calendars/p@x.com/events').mock(
            return_value=Response(200, json={'items': [src_event], 'nextSyncToken': 'tok-new'})
        )
        # Each target's mirror_key list returns empty (fresh) -> insert + patch on target side
        for email in ('a@x.com', 'b@x.com', 'n@x.com'):
            respx.get(f'{GOOGLE_API_BASE}/calendars/{email}/events').mock(
                return_value=Response(200, json={'items': []})
            )
            respx.post(f'{GOOGLE_API_BASE}/calendars/{email}/events').mock(
                return_value=Response(200, json={'id': f'mirror-on-{email}'})
            )
            respx.patch(f'{GOOGLE_API_BASE}/calendars/{email}/events/mirror-on-{email}').mock(
                return_value=Response(200, json={'id': f'mirror-on-{email}'})
            )

        r = app_client.post(
            '/webhook/google',
            headers={
                'X-Goog-Channel-Id': 'ch-1',
                'X-Goog-Channel-Token': 'secret-tok',
                'X-Goog-Resource-Id': 'res-1',
                'X-Goog-Resource-State': 'exists',
            },
        )

    assert r.status_code == 204

    # 3 mirrors created (busy on each work account; personal source -> work targets)
    with connect(settings.db_path) as conn:
        rows = event_links.find_all_for_source(
            conn,
            source_calendar_id=1,
            source_event_id='src-event-1',
        )
        assert len(rows) == 3
        assert {r['mode'] for r in rows} == {'busy'}

        # syncToken advanced
        cal = conn.execute('SELECT sync_token FROM calendars WHERE id = 1').fetchone()
        assert cal['sync_token'] == 'tok-new'
