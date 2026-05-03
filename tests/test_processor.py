"""Per-event orchestrator tests.

End-to-end exercise: matrix fan-out + skip filters + ensure_mirror +
patch on update. Google calls mocked with respx.
"""

import datetime as dt
import sqlite3
from pathlib import Path

import pytest
import respx
from httpx import Response

from calsync.config import DateWindow, NosyncConfig
from calsync.crypto import generate_hmac_key
from calsync.db import connect, init_db
from calsync.gapi.client import GOOGLE_API_BASE, GoogleCalendarClient
from calsync.repositories import event_links
from calsync.sync.matrix import DEFAULT_MATRIX
from calsync.sync.mirror import CalendarRef
from calsync.sync.processor import process_event

NOW = dt.datetime(2026, 5, 4, 12, 0, tzinfo=dt.UTC)


def _src(**overrides) -> dict:
    base = {
        'id': 'src-1',
        'summary': 'Real meeting',
        'description': 'agenda',
        'location': 'Room A',
        'start': {'dateTime': '2026-05-04T14:00:00-04:00'},
        'end': {'dateTime': '2026-05-04T15:00:00-04:00'},
    }
    base.update(overrides)
    return base


@pytest.fixture
def hmac_key():
    return generate_hmac_key()


@pytest.fixture
def db(tmp_path: Path):
    p = tmp_path / 'a.db'
    init_db(p)
    with connect(p) as conn:
        # Insert all 4 accounts in order to match the matrix labels.
        for i, (label, email) in enumerate(
            [('personal', 'p@x.com'), ('avela', 'a@x.com'), ('beachmedia', 'b@x.com'), ('novact', 'n@x.com')], start=1
        ):
            conn.execute(
                'INSERT INTO accounts (label, email, refresh_token_encrypted) VALUES (?, ?, ?)',
                (label, email, b'\x00'),
            )
            conn.execute('INSERT INTO calendars (account_id, google_calendar_id) VALUES (?, ?)', (i, email))
        yield conn


def _refs(db: sqlite3.Connection) -> dict[str, CalendarRef]:
    rows = db.execute(
        'SELECT a.label, c.id AS cal_id, c.google_calendar_id FROM accounts a '
        'JOIN calendars c ON c.account_id = a.id ORDER BY a.id'
    ).fetchall()
    return {
        r['label']: CalendarRef(db_id=r['cal_id'], google_calendar_id=r['google_calendar_id'], account_label=r['label'])
        for r in rows
    }


def _client():
    return GoogleCalendarClient(access_token='at', retry_base_wait=0.0, retry_max_wait=0.0)


def _ctx(db, hmac_key, **overrides):
    refs = _refs(db)
    base = {
        'conn': db,
        'targets_by_label': refs,
        'clients_by_label': {label: _client() for label in refs},
        'hmac_key': hmac_key,
        'nosync_config': NosyncConfig(),
        'date_window': DateWindow(mode='all'),
        'now': NOW,
        'matrix': DEFAULT_MATRIX,
    }
    base.update(overrides)
    return base


# --- happy path: avela source fans out to all 3 targets ---


async def test_avela_source_creates_3_mirrors_with_correct_modes(db: sqlite3.Connection, hmac_key):
    refs = _refs(db)
    src_event = _src()

    inserted = {}

    def _make_handler(target_label):
        def _handler(request):
            inserted[target_label] = request.content.decode()
            return Response(200, json={'id': f'mirror-on-{target_label}'})

        return _handler

    with respx.mock:
        # ensure_mirror's mirror_key list call returns empty for all 3 targets
        for label in ('personal', 'beachmedia', 'novact'):
            respx.get(f'{GOOGLE_API_BASE}/calendars/{refs[label].google_calendar_id}/events').mock(
                return_value=Response(200, json={'items': []})
            )
            respx.post(f'{GOOGLE_API_BASE}/calendars/{refs[label].google_calendar_id}/events').mock(
                side_effect=_make_handler(label)
            )
            respx.patch(f'{GOOGLE_API_BASE}/calendars/{refs[label].google_calendar_id}/events/mirror-on-{label}').mock(
                return_value=Response(200, json={'id': f'mirror-on-{label}'})
            )

        outcome = await process_event(
            source=refs['avela'],
            source_event=src_event,
            **_ctx(db, hmac_key),
        )

    by_target = {t.target_label: t for t in outcome.targets}
    assert by_target['personal'].action in ('updated', 'created')
    assert by_target['beachmedia'].action in ('updated', 'created')
    assert by_target['novact'].action in ('updated', 'created')

    # Personal got `full` mode (real summary in body); beachmedia and novact got `busy`
    assert 'Real meeting' in inserted['personal']
    assert '"summary":"Busy"' in inserted['beachmedia']
    assert '"summary":"Busy"' in inserted['novact']


# --- skip propagation ---


async def test_declined_event_skips_all_targets(db: sqlite3.Connection, hmac_key):
    refs = _refs(db)
    declined = _src(attendees=[{'email': 'me', 'self': True, 'responseStatus': 'declined'}])

    with respx.mock:
        outcome = await process_event(
            source=refs['avela'],
            source_event=declined,
            **_ctx(db, hmac_key),
        )

    assert all(t.action == 'skipped' for t in outcome.targets)
    assert all(t.reason == 'declined' for t in outcome.targets)


async def test_outside_window_skips_all_targets(db: sqlite3.Connection, hmac_key):
    refs = _refs(db)
    far_future = _src(start={'dateTime': '2030-01-01T10:00:00+00:00'})
    ctx = _ctx(db, hmac_key, date_window=DateWindow(mode='rolling', lookback_days=21, lookahead_days=180))

    with respx.mock:
        outcome = await process_event(
            source=refs['avela'],
            source_event=far_future,
            **ctx,
        )

    assert all(t.action == 'skipped' for t in outcome.targets)
    assert all(t.reason == 'outside_date_window' for t in outcome.targets)


# --- nosync scope=work mixed behavior ---


async def test_nosync_scope_work_skips_work_but_mirrors_to_personal(db: sqlite3.Connection, hmac_key):
    refs = _refs(db)
    tagged = _src(summary='[nosync] doctor visit')
    ctx = _ctx(db, hmac_key, nosync_config=NosyncConfig(tokens=['[nosync]'], scope='work'))

    inserted = {}

    def _on_personal_post(request):
        inserted['personal'] = request.content.decode()
        return Response(200, json={'id': 'mirror-personal'})

    with respx.mock:
        respx.get(f'{GOOGLE_API_BASE}/calendars/p@x.com/events').mock(return_value=Response(200, json={'items': []}))
        respx.post(f'{GOOGLE_API_BASE}/calendars/p@x.com/events').mock(side_effect=_on_personal_post)
        respx.patch(f'{GOOGLE_API_BASE}/calendars/p@x.com/events/mirror-personal').mock(
            return_value=Response(200, json={'id': 'mirror-personal'})
        )

        outcome = await process_event(
            source=refs['avela'],
            source_event=tagged,
            **ctx,
        )

    by_target = {t.target_label: t for t in outcome.targets}
    assert by_target['personal'].action in ('updated', 'created')
    assert 'doctor visit' in inserted['personal']
    assert by_target['beachmedia'].action == 'skipped'
    assert by_target['beachmedia'].reason.startswith('nosync_token:')
    assert by_target['novact'].action == 'skipped'


# --- cancellation ---


async def test_cancelled_source_deletes_existing_mirrors(db: sqlite3.Connection, hmac_key):
    refs = _refs(db)
    # Seed an event_links row for an existing mirror on beachmedia
    event_links.upsert(
        db,
        source_calendar_id=refs['avela'].db_id,
        source_event_id='src-1',
        mirror_calendar_id=refs['beachmedia'].db_id,
        mirror_event_id='to-be-deleted',
        mirror_key='k',
        mode='busy',
        source_start_at=NOW,
        source_end_at=NOW + dt.timedelta(hours=1),
    )

    cancelled = {'id': 'src-1', 'status': 'cancelled'}

    deleted: list[str] = []

    def _on_delete(request):
        deleted.append(str(request.url).rsplit('/', 1)[1])
        return Response(204)

    with respx.mock:
        respx.delete(f'{GOOGLE_API_BASE}/calendars/b@x.com/events/to-be-deleted').mock(side_effect=_on_delete)

        outcome = await process_event(
            source=refs['avela'],
            source_event=cancelled,
            **_ctx(db, hmac_key),
        )

    by_target = {t.target_label: t for t in outcome.targets}
    assert by_target['beachmedia'].action == 'deleted'
    # Other targets had no mirror -> noop
    assert by_target['personal'].action == 'noop'
    assert by_target['novact'].action == 'noop'

    # event_links row removed
    assert (
        event_links.find_by_source_target(
            db,
            source_calendar_id=refs['avela'].db_id,
            source_event_id='src-1',
            mirror_calendar_id=refs['beachmedia'].db_id,
        )
        is None
    )


# --- now-ineligible: source moved out of window after being mirrored ---


async def test_event_that_became_ineligible_has_mirror_deleted(db: sqlite3.Connection, hmac_key):
    refs = _refs(db)
    # Existing mirror on beachmedia for src-1
    event_links.upsert(
        db,
        source_calendar_id=refs['avela'].db_id,
        source_event_id='src-1',
        mirror_calendar_id=refs['beachmedia'].db_id,
        mirror_event_id='now-stale',
        mirror_key='k',
        mode='busy',
        source_start_at=NOW,
        source_end_at=NOW + dt.timedelta(hours=1),
    )

    # User just declined the meeting; it's the same source event, but now declined
    declined_now = _src(attendees=[{'email': 'me', 'self': True, 'responseStatus': 'declined'}])

    with respx.mock:
        respx.delete(f'{GOOGLE_API_BASE}/calendars/b@x.com/events/now-stale').mock(return_value=Response(204))

        outcome = await process_event(
            source=refs['avela'],
            source_event=declined_now,
            **_ctx(db, hmac_key),
        )

    by_target = {t.target_label: t for t in outcome.targets}
    assert by_target['beachmedia'].action == 'deleted'
    assert 'now_skipped' in by_target['beachmedia'].reason


# --- echo loop top-level guard ---


async def test_mirror_event_in_source_position_skips_everything(db: sqlite3.Connection, hmac_key):
    refs = _refs(db)
    mirror_event = _src(extendedProperties={'private': {'calsync_origin': 'calsync'}})

    outcome = await process_event(
        source=refs['avela'],
        source_event=mirror_event,
        **_ctx(db, hmac_key),
    )

    assert len(outcome.targets) == 1
    assert outcome.targets[0].action == 'skipped'
    assert outcome.targets[0].reason == 'echo_loop_calsync_origin'


# --- dry_run: no mutating calls ---


async def test_dry_run_makes_no_writes(db: sqlite3.Connection, hmac_key):
    refs = _refs(db)
    src_event = _src()
    ctx = _ctx(db, hmac_key, dry_run=True)

    with respx.mock:
        outcome = await process_event(
            source=refs['avela'],
            source_event=src_event,
            **ctx,
        )

    assert all(t.action == 'noop' for t in outcome.targets)
    assert all(t.reason == 'dry_run' for t in outcome.targets)
