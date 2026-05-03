"""Admin route tests: auth, cleanup, status, setup-watches."""

import datetime as dt

import pytest
import respx
from httpx import Response

from calsync.crypto import encrypt_token
from calsync.db import connect
from calsync.gapi.client import GOOGLE_API_BASE
from calsync.repositories import event_links, watch_channels


def _seed_4_accounts(settings):
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
            conn.execute('INSERT INTO calendars (account_id, google_calendar_id) VALUES (?, ?)', (i, email))


# --- auth ---


def test_admin_endpoints_require_token(app_client, settings):
    r = app_client.get('/admin/status')
    assert r.status_code == 401


def test_admin_endpoints_reject_wrong_token(app_client, settings):
    r = app_client.get('/admin/status', headers={'X-Admin-Token': 'wrong'})
    assert r.status_code == 401


@pytest.fixture
def admin_headers(settings):
    return {'X-Admin-Token': settings.admin_token}


# --- /status ---


def test_admin_status_lists_accounts(app_client, settings, admin_headers):
    _seed_4_accounts(settings)
    r = app_client.get('/admin/status', headers=admin_headers)
    assert r.status_code == 200
    data = r.json()
    labels = {a['label'] for a in data['accounts']}
    assert labels == {'personal', 'avela', 'beachmedia', 'novact'}
    assert data['event_links_count'] == 0
    assert all(a['needs_reauth'] is False for a in data['accounts'])


def test_admin_status_logs_to_audit(app_client, settings, admin_headers):
    _seed_4_accounts(settings)
    app_client.get('/admin/status', headers=admin_headers)
    with connect(settings.db_path) as conn:
        rows = conn.execute('SELECT * FROM admin_log ORDER BY id').fetchall()
    assert len(rows) == 1
    assert rows[0]['endpoint'] == '/admin/status'
    assert rows[0]['method'] == 'GET'
    assert rows[0]['result_code'] == 200


# --- /setup-watches ---


def test_admin_setup_watches_registers_channels(app_client, settings, admin_headers):
    _seed_4_accounts(settings)

    def _on_watch(target_email):
        def _h(_request):
            return Response(
                200,
                json={
                    'kind': 'api#channel',
                    'id': 'returned-id',  # Google echoes our id back
                    'resourceId': f'res-{target_email}',
                    'expiration': str(int((dt.datetime.now(dt.UTC) + dt.timedelta(days=7)).timestamp() * 1000)),
                },
            )

        return _h

    with respx.mock:
        for email in ('p@x.com', 'a@x.com', 'b@x.com', 'n@x.com'):
            respx.post(f'{GOOGLE_API_BASE}/calendars/{email}/events/watch').mock(side_effect=_on_watch(email))

        r = app_client.post('/admin/setup-watches', headers=admin_headers)

    assert r.status_code == 200
    summary = r.json()['channels']
    assert {s['account'] for s in summary} == {'personal', 'avela', 'beachmedia', 'novact'}
    assert all(s['status'] == 'ok' for s in summary)

    with connect(settings.db_path) as conn:
        active = conn.execute('SELECT calendar_id FROM watch_channels WHERE stopped_at IS NULL').fetchall()
        assert {a['calendar_id'] for a in active} == {1, 2, 3, 4}


# --- /cleanup/all ---


def test_admin_cleanup_all_deletes_calsync_events_and_drops_links(app_client, settings, admin_headers):
    _seed_4_accounts(settings)
    with connect(settings.db_path) as conn:
        # Seed a few event_links rows + sync tokens so we can verify they're cleared
        event_links.upsert(
            conn,
            source_calendar_id=1,
            source_event_id='src-1',
            mirror_calendar_id=2,
            mirror_event_id='m1',
            mirror_key='k1',
            mode='busy',
            source_start_at=dt.datetime(2026, 5, 4, tzinfo=dt.UTC),
            source_end_at=dt.datetime(2026, 5, 4, 1, tzinfo=dt.UTC),
        )
        conn.execute("UPDATE calendars SET sync_token = 'tok' WHERE id = 1")

    with respx.mock:
        # Each account: list returns one calsync event; delete returns 204
        for email in ('p@x.com', 'a@x.com', 'b@x.com', 'n@x.com'):
            respx.get(f'{GOOGLE_API_BASE}/calendars/{email}/events').mock(
                return_value=Response(200, json={'items': [{'id': f'mirror-on-{email}'}]})
            )
            respx.delete(f'{GOOGLE_API_BASE}/calendars/{email}/events/mirror-on-{email}').mock(
                return_value=Response(204)
            )

        r = app_client.post('/admin/cleanup/all', headers=admin_headers)

    assert r.status_code == 200
    assert r.json()['deleted_events_per_account'] == {'personal': 1, 'avela': 1, 'beachmedia': 1, 'novact': 1}

    with connect(settings.db_path) as conn:
        # event_links table is empty
        assert conn.execute('SELECT COUNT(*) AS n FROM event_links').fetchone()['n'] == 0
        # sync_token cleared
        assert conn.execute('SELECT sync_token FROM calendars WHERE id = 1').fetchone()['sync_token'] is None


# --- /cleanup/calendar ---


def test_admin_cleanup_calendar_self_contained_resume(app_client, settings, admin_headers):
    _seed_4_accounts(settings)
    with connect(settings.db_path) as conn:
        # Active watch channel on beachmedia (cal_id=3)
        watch_channels.replace_active(
            conn,
            calendar_id=3,
            channel_id='ch-bm',
            resource_id='res-bm',
            channel_token='t',
            callback_url='https://x/cb',
            expires_at=dt.datetime(2026, 5, 10, tzinfo=dt.UTC),
        )
        # event_links row pointing TO beachmedia
        event_links.upsert(
            conn,
            source_calendar_id=2,
            source_event_id='avela-evt',
            mirror_calendar_id=3,
            mirror_event_id='mir-bm',
            mirror_key='k',
            mode='busy',
            source_start_at=dt.datetime(2026, 5, 4, tzinfo=dt.UTC),
            source_end_at=dt.datetime(2026, 5, 4, 1, tzinfo=dt.UTC),
        )
        conn.execute("UPDATE calendars SET sync_token = 'tok' WHERE id = 3")

    with respx.mock:
        # stop_channel
        respx.post(f'{GOOGLE_API_BASE}/channels/stop').mock(return_value=Response(204))
        # list calsync events: returns the mirror
        respx.get(f'{GOOGLE_API_BASE}/calendars/b@x.com/events').mock(
            return_value=Response(200, json={'items': [{'id': 'mir-bm'}]})
        )
        respx.delete(f'{GOOGLE_API_BASE}/calendars/b@x.com/events/mir-bm').mock(return_value=Response(204))

        r = app_client.post('/admin/cleanup/calendar', headers=admin_headers, json={'account': 'beachmedia'})

    assert r.status_code == 200
    body = r.json()
    assert body['account'] == 'beachmedia'
    assert body['deleted_events'] == 1
    assert body['state'] == 'paused-then-resumed'

    with connect(settings.db_path) as conn:
        # account ends unpaused
        paused = conn.execute("SELECT paused FROM accounts WHERE label = 'beachmedia'").fetchone()['paused']
        assert paused == 0
        # watch channel marked stopped
        assert watch_channels.find_active_for_calendar(conn, 3) is None
        # event_links rows for this target removed
        rows = conn.execute('SELECT * FROM event_links WHERE mirror_calendar_id = 3').fetchall()
        assert rows == []
        # sync_token cleared
        assert conn.execute('SELECT sync_token FROM calendars WHERE id = 3').fetchone()['sync_token'] is None


# --- /cleanup/event ---


def test_admin_cleanup_event_deletes_one_source_mirrors(app_client, settings, admin_headers):
    _seed_4_accounts(settings)
    with connect(settings.db_path) as conn:
        for tgt_cal_id, email in [(2, 'a@x.com'), (3, 'b@x.com'), (4, 'n@x.com')]:
            event_links.upsert(
                conn,
                source_calendar_id=1,
                source_event_id='evt-1',
                mirror_calendar_id=tgt_cal_id,
                mirror_event_id=f'mir-{email}',
                mirror_key=f'k-{email}',
                mode='busy',
                source_start_at=dt.datetime(2026, 5, 4, tzinfo=dt.UTC),
                source_end_at=dt.datetime(2026, 5, 4, 1, tzinfo=dt.UTC),
            )

    with respx.mock:
        for email in ('a@x.com', 'b@x.com', 'n@x.com'):
            respx.delete(f'{GOOGLE_API_BASE}/calendars/{email}/events/mir-{email}').mock(return_value=Response(204))

        r = app_client.post(
            '/admin/cleanup/event',
            headers=admin_headers,
            json={'source_account': 'personal', 'source_event_id': 'evt-1'},
        )

    assert r.status_code == 200
    assert r.json()['deleted'] == 3

    with connect(settings.db_path) as conn:
        rows = event_links.find_all_for_source(conn, source_calendar_id=1, source_event_id='evt-1')
        assert rows == []
