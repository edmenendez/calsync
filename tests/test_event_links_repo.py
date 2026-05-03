"""event_links repository tests."""

import datetime as dt
import sqlite3
from pathlib import Path

import pytest

from calsync.db import connect, init_db
from calsync.repositories import event_links


@pytest.fixture
def db(tmp_path: Path):
    p = tmp_path / 'a.db'
    init_db(p)
    with connect(p) as conn:
        # Two accounts + their primary calendars (cal_ids 1 and 2).
        conn.execute("INSERT INTO accounts (label, email, refresh_token_encrypted) VALUES ('a', 'a@x', x'00')")
        conn.execute("INSERT INTO calendars (account_id, google_calendar_id) VALUES (1, 'a@x')")
        conn.execute("INSERT INTO accounts (label, email, refresh_token_encrypted) VALUES ('b', 'b@y', x'00')")
        conn.execute("INSERT INTO calendars (account_id, google_calendar_id) VALUES (2, 'b@y')")
        yield conn


def _now() -> dt.datetime:
    return dt.datetime(2026, 5, 3, 12, 0, 0, tzinfo=dt.UTC)


def test_upsert_inserts_when_missing(db: sqlite3.Connection):
    link_id = event_links.upsert(
        db,
        source_calendar_id=1,
        source_event_id='ev-1',
        mirror_calendar_id=2,
        mirror_event_id='mir-1',
        mirror_key='k1',
        mode='busy',
        source_start_at=_now(),
        source_end_at=_now() + dt.timedelta(hours=1),
    )
    assert link_id
    row = event_links.find_by_link_id(db, link_id)
    assert row is not None
    assert row['mirror_event_id'] == 'mir-1'
    assert row['mirror_key'] == 'k1'


def test_upsert_updates_when_exists_and_preserves_link_id(db: sqlite3.Connection):
    link_id_1 = event_links.upsert(
        db,
        source_calendar_id=1,
        source_event_id='ev-1',
        mirror_calendar_id=2,
        mirror_event_id='mir-1',
        mirror_key='k1',
        mode='busy',
        source_start_at=_now(),
        source_end_at=_now() + dt.timedelta(hours=1),
    )
    link_id_2 = event_links.upsert(
        db,
        source_calendar_id=1,
        source_event_id='ev-1',
        mirror_calendar_id=2,
        mirror_event_id='mir-2',
        mirror_key='k2',
        mode='full',
        source_start_at=_now(),
        source_end_at=_now() + dt.timedelta(hours=2),
    )
    assert link_id_1 == link_id_2  # link_id is stable across updates
    row = event_links.find_by_link_id(db, link_id_2)
    assert row['mirror_event_id'] == 'mir-2'
    assert row['mirror_key'] == 'k2'
    assert row['mode'] == 'full'


def test_find_by_source_target(db: sqlite3.Connection):
    event_links.upsert(
        db,
        source_calendar_id=1,
        source_event_id='ev-x',
        mirror_calendar_id=2,
        mirror_event_id='m',
        mirror_key='k',
        mode='busy',
        source_start_at=_now(),
        source_end_at=_now(),
    )
    found = event_links.find_by_source_target(
        db,
        source_calendar_id=1,
        source_event_id='ev-x',
        mirror_calendar_id=2,
    )
    assert found is not None
    assert found['mirror_event_id'] == 'm'

    missing = event_links.find_by_source_target(
        db,
        source_calendar_id=1,
        source_event_id='not-there',
        mirror_calendar_id=2,
    )
    assert missing is None


def test_find_by_mirror_event_for_echo_loop_check(db: sqlite3.Connection):
    """Echo-loop defense layer 2: lookup by mirror calendar+event id."""
    event_links.upsert(
        db,
        source_calendar_id=1,
        source_event_id='src',
        mirror_calendar_id=2,
        mirror_event_id='mirror-evt-id',
        mirror_key='k',
        mode='busy',
        source_start_at=_now(),
        source_end_at=_now(),
    )
    found = event_links.find_by_mirror_event(
        db,
        mirror_calendar_id=2,
        mirror_event_id='mirror-evt-id',
    )
    assert found is not None
    assert found['source_event_id'] == 'src'


def test_find_all_for_source_returns_every_target(db: sqlite3.Connection):
    """One source mirrored to multiple targets - find them all for fan-out."""
    db.execute("INSERT INTO accounts (label, email, refresh_token_encrypted) VALUES ('c', 'c@z', x'00')")
    db.execute("INSERT INTO calendars (account_id, google_calendar_id) VALUES (3, 'c@z')")

    for tgt_cal_id in (2, 3):
        event_links.upsert(
            db,
            source_calendar_id=1,
            source_event_id='ev-1',
            mirror_calendar_id=tgt_cal_id,
            mirror_event_id=f'mirror-on-{tgt_cal_id}',
            mirror_key=f'k{tgt_cal_id}',
            mode='busy',
            source_start_at=_now(),
            source_end_at=_now(),
        )

    rows = event_links.find_all_for_source(
        db,
        source_calendar_id=1,
        source_event_id='ev-1',
    )
    assert len(rows) == 2
    assert {r['mirror_calendar_id'] for r in rows} == {2, 3}


def test_delete_all_for_source_returns_count(db: sqlite3.Connection):
    db.execute("INSERT INTO accounts (label, email, refresh_token_encrypted) VALUES ('c', 'c@z', x'00')")
    db.execute("INSERT INTO calendars (account_id, google_calendar_id) VALUES (3, 'c@z')")
    for tgt_cal_id in (2, 3):
        event_links.upsert(
            db,
            source_calendar_id=1,
            source_event_id='ev-1',
            mirror_calendar_id=tgt_cal_id,
            mirror_event_id=f'm-{tgt_cal_id}',
            mirror_key=f'k{tgt_cal_id}',
            mode='busy',
            source_start_at=_now(),
            source_end_at=_now(),
        )

    count = event_links.delete_all_for_source(
        db,
        source_calendar_id=1,
        source_event_id='ev-1',
    )
    assert count == 2
    assert event_links.find_all_for_source(db, source_calendar_id=1, source_event_id='ev-1') == []


def test_delete_by_id(db: sqlite3.Connection):
    link_id = event_links.upsert(
        db,
        source_calendar_id=1,
        source_event_id='ev-1',
        mirror_calendar_id=2,
        mirror_event_id='m',
        mirror_key='k',
        mode='busy',
        source_start_at=_now(),
        source_end_at=_now(),
    )
    event_links.delete_by_id(db, link_id)
    assert event_links.find_by_link_id(db, link_id) is None
