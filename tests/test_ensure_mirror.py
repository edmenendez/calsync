"""ensure_mirror tests covering all four paths.

Mocks Google with respx so no network. Uses a real SQLite DB with the
real schema.
"""

import contextlib
import sqlite3
from pathlib import Path

import pytest
import respx
from httpx import Response

from calsync.crypto import derive_mirror_key, generate_hmac_key
from calsync.db import connect, init_db
from calsync.gapi.client import GOOGLE_API_BASE, GoogleCalendarClient
from calsync.repositories import event_links
from calsync.sync.mirror import CalendarRef, ensure_mirror

# Constants reused across tests
SRC_CAL = CalendarRef(db_id=1, google_calendar_id='ed@avela.org', account_label='avela')
TGT_CAL = CalendarRef(db_id=2, google_calendar_id='ed@beachmedia.io', account_label='beachmedia')

EVENTS_URL = f'{GOOGLE_API_BASE}/calendars/{TGT_CAL.google_calendar_id}/events'


def _src_event(event_id: str = 'src-event-1', summary: str = 'Quarterly review') -> dict:
    return {
        'id': event_id,
        'summary': summary,
        'description': 'private notes',
        'location': '123 Main',
        'start': {'dateTime': '2026-05-04T10:00:00-04:00', 'timeZone': 'America/New_York'},
        'end': {'dateTime': '2026-05-04T11:00:00-04:00', 'timeZone': 'America/New_York'},
    }


@pytest.fixture
def hmac_key() -> str:
    return generate_hmac_key()


@pytest.fixture
def db(tmp_path: Path):
    p = tmp_path / 'a.db'
    init_db(p)
    with connect(p) as conn:
        # Two accounts + their primary calendars (cal_ids 1 and 2 by insertion order).
        conn.execute(
            "INSERT INTO accounts (label, email, refresh_token_encrypted) VALUES ('avela', 'ed@avela.org', x'00')"
        )
        conn.execute("INSERT INTO calendars (account_id, google_calendar_id) VALUES (1, 'ed@avela.org')")
        conn.execute(
            'INSERT INTO accounts (label, email, refresh_token_encrypted) '
            "VALUES ('beachmedia', 'ed@beachmedia.io', x'00')"
        )
        conn.execute("INSERT INTO calendars (account_id, google_calendar_id) VALUES (2, 'ed@beachmedia.io')")
        yield conn


@pytest.fixture
def client() -> GoogleCalendarClient:
    return GoogleCalendarClient(access_token='at', retry_base_wait=0.0, retry_max_wait=0.0)


# --- Path 4: fresh insert ---


async def test_fresh_insert_when_nothing_exists(db: sqlite3.Connection, client, hmac_key):
    """DB empty + Google empty -> insert called, DB row written."""
    src = _src_event()
    inserted_body = {}

    def _on_insert(request):
        nonlocal inserted_body
        inserted_body = request.content.decode()
        return Response(200, json={'id': 'new-mirror-id', 'status': 'confirmed'})

    with respx.mock:
        respx.get(EVENTS_URL).mock(return_value=Response(200, json={'items': []}))
        respx.post(EVENTS_URL).mock(side_effect=_on_insert)

        link_id = await ensure_mirror(
            conn=db,
            client=client,
            hmac_key=hmac_key,
            source=SRC_CAL,
            target=TGT_CAL,
            source_event=src,
            mode='busy',
        )

    assert link_id  # got a uuid
    row = event_links.find_by_link_id(db, link_id)
    assert row['mirror_event_id'] == 'new-mirror-id'
    assert row['mode'] == 'busy'
    # Mirror_key stamped into the body matches what we'd derive
    expected_key = derive_mirror_key(
        source_google_calendar_id=SRC_CAL.google_calendar_id,
        source_event_id='src-event-1',
        target_google_calendar_id=TGT_CAL.google_calendar_id,
        mode='busy',
        hmac_key=hmac_key,
    )
    assert expected_key in inserted_body
    assert 'calsync_origin' in inserted_body


# --- Path 1: DB fast path (already mirrored, healthy) ---


async def test_db_fast_path_returns_existing_when_mirror_alive(db: sqlite3.Connection, client, hmac_key):
    """Pre-existing event_links row + Google mirror still alive -> no API mutation."""
    src = _src_event()
    # Seed an event_links row pointing at an extant mirror.
    expected_key = derive_mirror_key(
        source_google_calendar_id=SRC_CAL.google_calendar_id,
        source_event_id=src['id'],
        target_google_calendar_id=TGT_CAL.google_calendar_id,
        mode='busy',
        hmac_key=hmac_key,
    )
    seeded_link_id = event_links.upsert(
        db,
        source_calendar_id=SRC_CAL.db_id,
        source_event_id=src['id'],
        mirror_calendar_id=TGT_CAL.db_id,
        mirror_event_id='preexisting-mirror',
        mirror_key=expected_key,
        mode='busy',
        source_start_at=__import__('datetime').datetime(2026, 5, 4, tzinfo=__import__('datetime').UTC),
        source_end_at=__import__('datetime').datetime(2026, 5, 4, 1, tzinfo=__import__('datetime').UTC),
    )

    with respx.mock:
        # get_event for the existing mirror returns 200
        get_route = respx.get(f'{EVENTS_URL}/preexisting-mirror').mock(
            return_value=Response(200, json={'id': 'preexisting-mirror'})
        )
        # No insert, no list, no other calls expected
        insert_route = respx.post(EVENTS_URL).mock(return_value=Response(500, json={'error': 'should not be called'}))

        link_id = await ensure_mirror(
            conn=db,
            client=client,
            hmac_key=hmac_key,
            source=SRC_CAL,
            target=TGT_CAL,
            source_event=src,
            mode='busy',
        )

    assert link_id == seeded_link_id
    assert get_route.called
    assert not insert_route.called


# --- Path 1b: DB row stale (Google mirror was deleted) ---


async def test_db_fast_path_falls_through_when_mirror_404s(db: sqlite3.Connection, client, hmac_key):
    """User manually deleted the mirror; we recreate it."""
    src = _src_event()
    expected_key = derive_mirror_key(
        source_google_calendar_id=SRC_CAL.google_calendar_id,
        source_event_id=src['id'],
        target_google_calendar_id=TGT_CAL.google_calendar_id,
        mode='busy',
        hmac_key=hmac_key,
    )
    event_links.upsert(
        db,
        source_calendar_id=SRC_CAL.db_id,
        source_event_id=src['id'],
        mirror_calendar_id=TGT_CAL.db_id,
        mirror_event_id='deleted-mirror',
        mirror_key=expected_key,
        mode='busy',
        source_start_at=__import__('datetime').datetime(2026, 5, 4, tzinfo=__import__('datetime').UTC),
        source_end_at=__import__('datetime').datetime(2026, 5, 4, 1, tzinfo=__import__('datetime').UTC),
    )

    with respx.mock:
        respx.get(f'{EVENTS_URL}/deleted-mirror').mock(
            return_value=Response(404, json={'error': {'message': 'Not Found'}})
        )
        # mirror_key check returns empty
        respx.get(EVENTS_URL).mock(return_value=Response(200, json={'items': []}))
        # insert is called for the recreate
        respx.post(EVENTS_URL).mock(return_value=Response(200, json={'id': 'recreated-mirror'}))

        await ensure_mirror(
            conn=db,
            client=client,
            hmac_key=hmac_key,
            source=SRC_CAL,
            target=TGT_CAL,
            source_event=src,
            mode='busy',
        )

    row = event_links.find_by_source_target(
        db,
        source_calendar_id=SRC_CAL.db_id,
        source_event_id=src['id'],
        mirror_calendar_id=TGT_CAL.db_id,
    )
    assert row['mirror_event_id'] == 'recreated-mirror'


# --- Path 2: orphan adoption ---


async def test_orphan_adoption_when_google_has_mirror_but_db_does_not(db: sqlite3.Connection, client, hmac_key):
    """Process crashed mid-insert: mirror exists on Google but no DB row.
    On retry, mirror_key check finds it and adopts it without re-inserting.
    """
    src = _src_event()
    expected_key = derive_mirror_key(
        source_google_calendar_id=SRC_CAL.google_calendar_id,
        source_event_id=src['id'],
        target_google_calendar_id=TGT_CAL.google_calendar_id,
        mode='busy',
        hmac_key=hmac_key,
    )

    with respx.mock:
        respx.get(EVENTS_URL).mock(
            return_value=Response(
                200,
                json={
                    'items': [
                        {
                            'id': 'orphan-mirror',
                            'updated': '2026-05-03T12:00:00.000Z',
                            'extendedProperties': {
                                'private': {
                                    'calsync_origin': 'calsync',
                                    'calsync_mirror_key': expected_key,
                                }
                            },
                        }
                    ]
                },
            )
        )
        # NO insert should fire
        insert_route = respx.post(EVENTS_URL).mock(return_value=Response(500, json={'error': 'should not be called'}))

        link_id = await ensure_mirror(
            conn=db,
            client=client,
            hmac_key=hmac_key,
            source=SRC_CAL,
            target=TGT_CAL,
            source_event=src,
            mode='busy',
        )

    assert not insert_route.called
    row = event_links.find_by_link_id(db, link_id)
    assert row['mirror_event_id'] == 'orphan-mirror'


# --- Path 3: multi-match repair ---


async def test_multi_match_picks_newest_and_deletes_losers(db: sqlite3.Connection, client, hmac_key):
    src = _src_event()
    deleted: list[str] = []

    def _on_delete(request):
        deleted.append(str(request.url).rsplit('/', 1)[1])
        return Response(204)

    with respx.mock:
        respx.get(EVENTS_URL).mock(
            return_value=Response(
                200,
                json={
                    'items': [
                        {'id': 'older-mirror', 'updated': '2026-05-01T10:00:00.000Z'},
                        {'id': 'newest-mirror', 'updated': '2026-05-03T12:00:00.000Z'},
                        {'id': 'oldest-mirror', 'updated': '2026-04-25T10:00:00.000Z'},
                    ]
                },
            )
        )
        respx.delete(f'{EVENTS_URL}/older-mirror').mock(side_effect=_on_delete)
        respx.delete(f'{EVENTS_URL}/oldest-mirror').mock(side_effect=_on_delete)
        # No insert needed
        respx.post(EVENTS_URL).mock(return_value=Response(500, json={'error': 'should not be called'}))

        link_id = await ensure_mirror(
            conn=db,
            client=client,
            hmac_key=hmac_key,
            source=SRC_CAL,
            target=TGT_CAL,
            source_event=src,
            mode='busy',
        )

    row = event_links.find_by_link_id(db, link_id)
    assert row['mirror_event_id'] == 'newest-mirror'
    assert set(deleted) == {'older-mirror', 'oldest-mirror'}


async def test_multi_match_loser_404_swallowed(db: sqlite3.Connection, client, hmac_key):
    """If a loser's delete returns 404 (already gone), keep going."""
    src = _src_event()
    with respx.mock:
        respx.get(EVENTS_URL).mock(
            return_value=Response(
                200,
                json={
                    'items': [
                        {'id': 'winner', 'updated': '2026-05-03T12:00:00.000Z'},
                        {'id': 'already-gone-loser', 'updated': '2026-05-01T10:00:00.000Z'},
                    ]
                },
            )
        )
        respx.delete(f'{EVENTS_URL}/already-gone-loser').mock(
            return_value=Response(404, json={'error': {'message': 'Not Found'}})
        )

        link_id = await ensure_mirror(
            conn=db,
            client=client,
            hmac_key=hmac_key,
            source=SRC_CAL,
            target=TGT_CAL,
            source_event=src,
            mode='busy',
        )
    row = event_links.find_by_link_id(db, link_id)
    assert row['mirror_event_id'] == 'winner'


# --- mirror_key uses Google IDs not DB PKs ---


async def test_mirror_key_uses_google_ids_not_db_pks(db: sqlite3.Connection, client, hmac_key):
    """Same source mirrored after DB rebuild produces same mirror_key."""
    src = _src_event()
    captured_keys = []

    def _capture_list(request):
        captured_keys.append(str(request.url))
        return Response(200, json={'items': []})

    def _on_insert(request):
        return Response(200, json={'id': 'new-mirror'})

    with respx.mock:
        respx.get(EVENTS_URL).mock(side_effect=_capture_list)
        respx.post(EVENTS_URL).mock(side_effect=_on_insert)

        # First call with normal db_ids
        await ensure_mirror(
            conn=db,
            client=client,
            hmac_key=hmac_key,
            source=SRC_CAL,
            target=TGT_CAL,
            source_event=src,
            mode='busy',
        )
        # Second call with completely different db_ids but same Google IDs
        # Simulates DB rebuild where SQLite PKs are different but Google IDs are stable.
        spoofed_src = CalendarRef(db_id=999, google_calendar_id=SRC_CAL.google_calendar_id, account_label='avela')
        spoofed_tgt = CalendarRef(db_id=998, google_calendar_id=TGT_CAL.google_calendar_id, account_label='beachmedia')
        # Won't actually create a link due to FK violation on cal_id 998/999, but that's OK
        # for this test - we only care that the mirror_key URL is identical.
        with contextlib.suppress(sqlite3.IntegrityError):
            await ensure_mirror(
                conn=db,
                client=client,
                hmac_key=hmac_key,
                source=spoofed_src,
                target=spoofed_tgt,
                source_event=src,
                mode='busy',
            )

    # Both calls should have queried the same mirror_key
    assert len(captured_keys) >= 2
    assert captured_keys[0] == captured_keys[1]


# --- full mode integration ---


async def test_full_mode_passes_color_through(db: sqlite3.Connection, client, hmac_key):
    src = _src_event()
    captured = {}

    def _on_insert(request):
        captured['body'] = request.content.decode()
        return Response(200, json={'id': 'mirror-1'})

    with respx.mock:
        respx.get(EVENTS_URL).mock(return_value=Response(200, json={'items': []}))
        respx.post(EVENTS_URL).mock(side_effect=_on_insert)

        await ensure_mirror(
            conn=db,
            client=client,
            hmac_key=hmac_key,
            source=SRC_CAL,
            target=TGT_CAL,
            source_event=src,
            mode='full',
        )

    assert '"colorId":"3"' in captured['body']  # avela -> Grape (purple)
    assert 'Quarterly review' in captured['body']  # full title preserved
