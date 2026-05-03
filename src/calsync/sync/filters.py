"""Skip filters: source-event predicates that decide whether to mirror.

Each predicate has a reason string for logging. The composite `should_skip`
returns the FIRST reason that matches, so reasons can be used as a
spec-by-example for behavior priority.

Order of checks (top to bottom; first match wins):
1. echo loop  - the event itself is a calsync mirror
2. cancelled  - tombstone; caller should delete instead of skip
3. nosync     - opt-out token in title/description (respects scope)
4. all-day    - v1 limitation
5. recurring  - master event with RRULE/RDATE/EXDATE; v1 skips entirely
6. transparent - "Show as: Free" on source means we shouldn't mirror busy
7. declined / tentative - source's RSVP is No / Maybe
8. window     - event start outside the configured date window
"""

import datetime as dt
from dataclasses import dataclass
from typing import Literal
from zoneinfo import ZoneInfo

from calsync.config import DateWindow, NosyncConfig

CALSYNC_ORIGIN_MARKER = 'calsync'


@dataclass(frozen=True)
class SkipResult:
    skip: bool
    reason: str = ''


def is_mirror(event: dict) -> bool:
    """Echo-loop primary check.

    True if event was created by calsync (has calsync_origin extended
    property). The webhook handler should additionally consult
    event_links.find_by_mirror_event as defense in depth, but this
    cheap check catches the common case without a DB hit.
    """
    private = event.get('extendedProperties', {}).get('private', {})
    return private.get('calsync_origin') == CALSYNC_ORIGIN_MARKER


def is_cancelled(event: dict) -> bool:
    """Tombstone from events.list(showDeleted=True). Caller deletes mirrors."""
    return event.get('status') == 'cancelled'


def _self_attendee_response(event: dict) -> str | None:
    """Find attendees[].self == true and return its responseStatus.

    Per the design feedback: prefer self==true over email matching to
    handle aliases, organizer-as-attendee cases, and Workspace
    email-vs-display-email mismatches. Returns None if no self entry
    exists OR no attendees list at all - the caller treats both as
    'eligible to mirror' (the user is implicitly attending events they
    created themselves).
    """
    attendees = event.get('attendees') or []
    for a in attendees:
        if a.get('self') is True:
            return a.get('responseStatus')
    return None


def _matches_nosync_token(event: dict, tokens: list[str]) -> str | None:
    """Return the first matching token (case-insensitive substring) or None."""
    haystack = ((event.get('summary') or '') + ' ' + (event.get('description') or '')).lower()
    for token in tokens:
        if token.lower() in haystack:
            return token
    return None


def _is_all_day(event: dict) -> bool:
    """All-day events have only `date`, no `dateTime`."""
    start = event.get('start') or {}
    return 'date' in start and 'dateTime' not in start


def _is_recurring_master(event: dict) -> bool:
    """Master events carry a `recurrence` array (RRULE / RDATE / EXDATE).

    Modified instances of recurring events have `recurringEventId` set
    but NO `recurrence` field; those pass through this filter as normal
    one-off events.
    """
    return bool(event.get('recurrence'))


def _resolve_window_bounds(window: DateWindow, now: dt.datetime) -> tuple[dt.datetime | None, dt.datetime | None]:
    """Compute (lower, upper) UTC bounds for the configured window mode.

    `None` means unbounded on that side. `mode='all'` returns (None, None).
    """
    if window.mode == 'all':
        return (None, None)

    if window.mode == 'rolling':
        lo = now - dt.timedelta(days=window.lookback_days)
        hi = now + dt.timedelta(days=window.lookahead_days)
        return (lo, hi)

    if window.mode == 'absolute':
        lo = dt.datetime.fromisoformat(window.absolute_start) if window.absolute_start else None
        hi = dt.datetime.fromisoformat(window.absolute_end) if window.absolute_end else None
        return (lo, hi)

    # mode == 'current_week'
    tz = ZoneInfo(window.timezone)
    local_now = now.astimezone(tz)
    # Monday=0 ... Sunday=6 in Python's weekday().
    # week_starts_on='monday': Mon=0 -> 0 days back
    # week_starts_on='sunday':  Sun=6 -> 6 days back from Sunday
    days_since_start = local_now.weekday() if window.week_starts_on == 'monday' else (local_now.weekday() + 1) % 7
    week_start_local = local_now.replace(hour=0, minute=0, second=0, microsecond=0) - dt.timedelta(
        days=days_since_start
    )
    week_end_local = week_start_local + dt.timedelta(days=7) - dt.timedelta(microseconds=1)
    return (week_start_local.astimezone(dt.UTC), week_end_local.astimezone(dt.UTC))


def _event_start_to_utc(event: dt.datetime | dict) -> dt.datetime | None:
    """Best-effort UTC conversion of an event's start. Returns None for date-only."""
    start = event.get('start') if isinstance(event, dict) else event
    if not start:
        return None
    if 'dateTime' in start:
        d = dt.datetime.fromisoformat(start['dateTime'])
        if d.tzinfo is None:
            d = d.replace(tzinfo=dt.UTC)
        return d.astimezone(dt.UTC)
    if 'date' in start:
        return dt.datetime.fromisoformat(start['date'] + 'T00:00:00+00:00')
    return None


def is_within_window(event: dict, window: DateWindow, now: dt.datetime) -> bool:
    """True iff the event's start time falls inside the configured window.

    Per the design's intentional tradeoff: only start time is checked.
    Events that start inside the window but end outside still mirror;
    events that start before but end inside do NOT.
    """
    lo, hi = _resolve_window_bounds(window, now)
    start_utc = _event_start_to_utc(event)
    if start_utc is None:
        return False  # we can't decide; conservatively exclude
    if lo is not None and start_utc < lo:
        return False
    return not (hi is not None and start_utc > hi)


def should_skip(
    source_event: dict,
    *,
    source_account_label: str,
    target_account_label: str,
    personal_account_label: str,
    mode: Literal['full', 'busy'],
    nosync_config: NosyncConfig,
    date_window: DateWindow,
    now: dt.datetime,
) -> SkipResult:
    """Decide whether to mirror this source event onto this target.

    Returns SkipResult(skip=True, reason=<short string>) on the first
    rule match; SkipResult(skip=False) if all checks pass.

    `target_account_label` is needed because the nosync scope='work'
    rule mirrors to personal but skips work accounts, so the same source
    event can be skipped on some targets and mirrored on others.
    """
    if is_mirror(source_event):
        return SkipResult(True, 'echo_loop_calsync_origin')

    if is_cancelled(source_event):
        return SkipResult(True, 'cancelled')

    nosync_token = _matches_nosync_token(source_event, nosync_config.tokens)
    if nosync_token is not None and (nosync_config.scope == 'all' or target_account_label != personal_account_label):
        return SkipResult(True, f'nosync_token:{nosync_token}')

    if _is_all_day(source_event):
        return SkipResult(True, 'all_day_event_v1_skip')

    if _is_recurring_master(source_event):
        return SkipResult(True, 'recurring_master_v1_skip')

    if source_event.get('transparency') == 'transparent':
        return SkipResult(True, 'transparent_free')

    response_status = _self_attendee_response(source_event)
    if response_status == 'declined':
        return SkipResult(True, 'declined')
    if response_status == 'tentative':
        return SkipResult(True, 'tentative')

    if not is_within_window(source_event, date_window, now):
        return SkipResult(True, 'outside_date_window')

    return SkipResult(False)
