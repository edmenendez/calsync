"""Skip filter tests covering every reason in priority order."""

import datetime as dt

from calsync.config import DateWindow, NosyncConfig
from calsync.sync.filters import (
    is_cancelled,
    is_mirror,
    is_within_window,
    should_skip,
)


def _src(**overrides) -> dict:
    base = {
        'id': 'src-1',
        'summary': 'Real meeting',
        'description': '',
        'start': {'dateTime': '2026-05-04T10:00:00-04:00'},
        'end': {'dateTime': '2026-05-04T11:00:00-04:00'},
    }
    base.update(overrides)
    return base


def _common_kwargs(**overrides):
    base = {
        'source_account_label': 'avela',
        'target_account_label': 'beachmedia',
        'personal_account_label': 'personal',
        'mode': 'busy',
        'nosync_config': NosyncConfig(),
        'date_window': DateWindow(mode='all'),
        'now': dt.datetime(2026, 5, 4, 12, 0, tzinfo=dt.UTC),
    }
    base.update(overrides)
    return base


# --- is_mirror ---


def test_is_mirror_true_when_calsync_origin_present():
    e = {'extendedProperties': {'private': {'calsync_origin': 'calsync'}}}
    assert is_mirror(e) is True


def test_is_mirror_false_when_no_extended_properties():
    assert is_mirror({}) is False


def test_is_mirror_false_when_other_origin():
    e = {'extendedProperties': {'private': {'calsync_origin': 'other_app'}}}
    assert is_mirror(e) is False


# --- is_cancelled ---


def test_is_cancelled_for_tombstone():
    assert is_cancelled({'status': 'cancelled'}) is True


def test_is_cancelled_false_for_normal():
    assert is_cancelled({'status': 'confirmed'}) is False
    assert is_cancelled({}) is False


# --- echo loop -> skip ---


def test_skip_echo_loop_first_priority():
    """Even an event that would otherwise pass MUST be skipped if it's our mirror."""
    e = _src(extendedProperties={'private': {'calsync_origin': 'calsync'}})
    r = should_skip(e, **_common_kwargs())
    assert r.skip
    assert r.reason == 'echo_loop_calsync_origin'


# --- cancelled tombstones ---


def test_cancelled_returns_skip_with_reason():
    """Cancelled events are 'skipped' here; webhook handler dispatches to delete logic."""
    e = _src(status='cancelled')
    r = should_skip(e, **_common_kwargs())
    assert r.skip
    assert r.reason == 'cancelled'


# --- all-day ---


def test_all_day_event_skipped():
    e = _src(start={'date': '2026-05-04'}, end={'date': '2026-05-05'})
    r = should_skip(e, **_common_kwargs())
    assert r.skip
    assert r.reason == 'all_day_event_v1_skip'


# --- recurring master ---


def test_recurring_master_skipped():
    e = _src(recurrence=['RRULE:FREQ=WEEKLY;BYDAY=MO'])
    r = should_skip(e, **_common_kwargs())
    assert r.skip
    assert r.reason == 'recurring_master_v1_skip'


def test_modified_instance_not_skipped():
    """Recurring exceptions have recurringEventId but NOT recurrence; should pass."""
    e = _src(recurringEventId='parent-master-id', originalStartTime={'dateTime': '2026-05-04T10:00:00-04:00'})
    r = should_skip(e, **_common_kwargs())
    assert not r.skip


# --- transparent ---


def test_transparent_event_skipped():
    e = _src(transparency='transparent')
    r = should_skip(e, **_common_kwargs())
    assert r.skip
    assert r.reason == 'transparent_free'


def test_opaque_event_passes():
    e = _src(transparency='opaque')
    r = should_skip(e, **_common_kwargs())
    assert not r.skip


# --- declined / tentative via attendees[].self ---


def test_declined_self_attendee_skipped():
    e = _src(
        attendees=[
            {'email': 'someone@x.com', 'self': False, 'responseStatus': 'accepted'},
            {'email': 'me@avela.org', 'self': True, 'responseStatus': 'declined'},
        ]
    )
    r = should_skip(e, **_common_kwargs())
    assert r.skip
    assert r.reason == 'declined'


def test_tentative_self_attendee_skipped():
    e = _src(attendees=[{'email': 'me', 'self': True, 'responseStatus': 'tentative'}])
    r = should_skip(e, **_common_kwargs())
    assert r.skip
    assert r.reason == 'tentative'


def test_accepted_self_attendee_passes():
    e = _src(attendees=[{'email': 'me', 'self': True, 'responseStatus': 'accepted'}])
    r = should_skip(e, **_common_kwargs())
    assert not r.skip


def test_needs_action_self_attendee_passes():
    """needsAction (not yet replied) is treated as eligible per the design."""
    e = _src(attendees=[{'email': 'me', 'self': True, 'responseStatus': 'needsAction'}])
    r = should_skip(e, **_common_kwargs())
    assert not r.skip


def test_no_attendees_passes():
    """User-created events with no other invitees: implicitly attending."""
    e = _src()  # no attendees field
    r = should_skip(e, **_common_kwargs())
    assert not r.skip


def test_no_self_attendee_passes():
    """attendees exists but no self entry (e.g., shared from outside): treat as eligible."""
    e = _src(attendees=[{'email': 'someone-else@x.com', 'responseStatus': 'accepted'}])
    r = should_skip(e, **_common_kwargs())
    assert not r.skip


# --- nosync token ---


def test_nosync_token_in_title_skipped_default_scope_all():
    e = _src(summary='[nosync] doctor appt')
    r = should_skip(e, **_common_kwargs())
    assert r.skip
    assert r.reason.startswith('nosync_token:')


def test_nosync_token_in_description_skipped():
    e = _src(description='Plan stuff [private] keep on calendar only')
    r = should_skip(e, **_common_kwargs())
    assert r.skip


def test_nosync_token_case_insensitive():
    e = _src(summary='[NoSync] confidential')
    r = should_skip(e, **_common_kwargs())
    assert r.skip


def test_nosync_scope_work_skips_work_target():
    e = _src(summary='[nosync] doctor')
    r = should_skip(
        e,
        **_common_kwargs(
            target_account_label='beachmedia',  # work
            nosync_config=NosyncConfig(scope='work'),
        ),
    )
    assert r.skip


def test_nosync_scope_work_passes_for_personal_target():
    """scope='work' means personal still gets the full mirror."""
    e = _src(summary='[nosync] doctor')
    r = should_skip(
        e,
        **_common_kwargs(
            target_account_label='personal',
            nosync_config=NosyncConfig(scope='work'),
        ),
    )
    assert not r.skip


def test_nosync_custom_token_list():
    e = _src(summary='hush hush')
    r = should_skip(
        e,
        **_common_kwargs(nosync_config=NosyncConfig(tokens=['hush'])),
    )
    assert r.skip


# --- date window ---


def test_outside_window_rolling_skipped():
    e = _src(start={'dateTime': '2030-01-01T10:00:00+00:00'})
    r = should_skip(
        e,
        **_common_kwargs(
            date_window=DateWindow(mode='rolling', lookback_days=21, lookahead_days=180),
        ),
    )
    assert r.skip
    assert r.reason == 'outside_date_window'


def test_inside_window_rolling_passes():
    e = _src(start={'dateTime': '2026-05-04T10:00:00+00:00'})
    r = should_skip(
        e,
        **_common_kwargs(
            date_window=DateWindow(mode='rolling', lookback_days=21, lookahead_days=180),
        ),
    )
    assert not r.skip


def test_window_mode_all_never_skips_on_window():
    """`all` mode: any future or past event is in window."""
    e = _src(start={'dateTime': '2050-01-01T10:00:00+00:00'})
    r = should_skip(e, **_common_kwargs(date_window=DateWindow(mode='all')))
    assert not r.skip


def test_window_mode_current_week_picks_correct_bounds():
    """now=Tuesday May 5 2026 (UTC); current_week (Mon-Sun, NY tz)
    should include Mon May 4 - Sun May 10 in local time."""
    window = DateWindow(mode='current_week', timezone='America/New_York', week_starts_on='monday')
    now = dt.datetime(2026, 5, 5, 17, 0, tzinfo=dt.UTC)  # Tue 1pm ET
    # Mon May 4 ET 10am should be in window
    assert is_within_window(_src(start={'dateTime': '2026-05-04T10:00:00-04:00'}), window, now)
    # Sun May 10 ET 11pm in window
    assert is_within_window(_src(start={'dateTime': '2026-05-10T23:00:00-04:00'}), window, now)
    # Sun Apr 26 (last week) NOT in window
    assert not is_within_window(_src(start={'dateTime': '2026-04-26T15:00:00-04:00'}), window, now)
    # Mon May 11 (next week) NOT in window
    assert not is_within_window(_src(start={'dateTime': '2026-05-11T10:00:00-04:00'}), window, now)


def test_window_only_checks_start_time_per_design():
    """Event starting INSIDE window but ending after window's end still mirrors."""
    window = DateWindow(
        mode='absolute', absolute_start='2026-05-01T00:00:00+00:00', absolute_end='2026-05-08T00:00:00+00:00'
    )
    now = dt.datetime(2026, 5, 4, tzinfo=dt.UTC)
    e = _src(
        start={'dateTime': '2026-05-07T23:00:00+00:00'},
        end={'dateTime': '2026-05-09T01:00:00+00:00'},  # ends after window
    )
    assert is_within_window(e, window, now)


# --- priority ordering: echo loop wins over everything ---


def test_priority_order_echo_loop_beats_other_skips():
    """Even all-day, declined, etc. - mirror events skip with the echo reason."""
    e = _src(
        start={'date': '2026-05-04'},
        end={'date': '2026-05-05'},
        extendedProperties={'private': {'calsync_origin': 'calsync'}},
    )
    r = should_skip(e, **_common_kwargs())
    assert r.skip
    assert r.reason == 'echo_loop_calsync_origin'  # not 'all_day_event_v1_skip'


# --- happy path: nothing matches ---


def test_normal_event_passes_all_filters():
    e = _src(
        attendees=[{'email': 'me@avela.org', 'self': True, 'responseStatus': 'accepted'}],
        transparency='opaque',
    )
    r = should_skip(e, **_common_kwargs())
    assert not r.skip
    assert r.reason == ''
