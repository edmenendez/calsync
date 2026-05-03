"""GoogleCalendarClient read-path tests.

Uses respx to mock Google's REST endpoints; no network calls.
"""

import pytest
import respx
from httpx import Response

from calsync.gapi.client import GOOGLE_API_BASE, GoogleCalendarClient
from calsync.gapi.errors import GoneError, GoogleApiError, NotFoundError, RateLimitError


@pytest.fixture
def client() -> GoogleCalendarClient:
    return GoogleCalendarClient(access_token='at-test')


# --- list_events ---


async def test_list_events_basic(client):
    with respx.mock:
        respx.get(f'{GOOGLE_API_BASE}/calendars/test%40x.com/events').mock(
            return_value=Response(
                200,
                json={'items': [{'id': 'e1'}], 'nextSyncToken': 'tok-1'},
            )
        )
        result = await client.list_events('test@x.com')
    assert result['items'] == [{'id': 'e1'}]
    assert result['nextSyncToken'] == 'tok-1'


async def test_list_events_includes_show_deleted_true_by_default(client):
    """showDeleted=true is mandatory: source-event tombstones must reach the processor."""
    captured = {}

    def _capture(request):
        captured['url'] = str(request.url)
        return Response(200, json={'items': []})

    with respx.mock:
        respx.get(f'{GOOGLE_API_BASE}/calendars/x/events').mock(side_effect=_capture)
        await client.list_events('x')
    assert 'showDeleted=true' in captured['url']


async def test_list_events_passes_sync_token(client):
    captured = {}

    def _capture(request):
        captured['url'] = str(request.url)
        return Response(200, json={'items': []})

    with respx.mock:
        respx.get(f'{GOOGLE_API_BASE}/calendars/x/events').mock(side_effect=_capture)
        await client.list_events('x', sync_token='tok-abc')
    assert 'syncToken=tok-abc' in captured['url']


async def test_list_events_passes_private_extended_property(client):
    """For idempotent mirror_key lookups."""
    captured = {}

    def _capture(request):
        captured['url'] = str(request.url)
        return Response(200, json={'items': []})

    with respx.mock:
        respx.get(f'{GOOGLE_API_BASE}/calendars/x/events').mock(side_effect=_capture)
        await client.list_events(
            'x',
            private_extended_property=['calsync_mirror_key=abc123'],
        )
    assert 'privateExtendedProperty=calsync_mirror_key%3Dabc123' in captured['url']


async def test_list_events_passes_time_window(client):
    captured = {}

    def _capture(request):
        captured['url'] = str(request.url)
        return Response(200, json={'items': []})

    with respx.mock:
        respx.get(f'{GOOGLE_API_BASE}/calendars/x/events').mock(side_effect=_capture)
        await client.list_events(
            'x',
            time_min='2026-05-01T00:00:00Z',
            time_max='2026-05-08T00:00:00Z',
        )
    assert 'timeMin=2026-05-01T00%3A00%3A00Z' in captured['url']
    assert 'timeMax=2026-05-08T00%3A00%3A00Z' in captured['url']


async def test_list_events_410_raises_gone_error(client):
    """syncToken expired -> caller must do a full re-list."""
    with respx.mock:
        respx.get(f'{GOOGLE_API_BASE}/calendars/x/events').mock(
            return_value=Response(
                410,
                json={'error': {'code': 410, 'message': 'Sync token is no longer valid'}},
            )
        )
        with pytest.raises(GoneError, match='no longer valid'):
            await client.list_events('x', sync_token='stale')


async def test_list_events_404_raises_not_found_error(client):
    with respx.mock:
        respx.get(f'{GOOGLE_API_BASE}/calendars/x/events').mock(
            return_value=Response(404, json={'error': {'code': 404, 'message': 'Not Found'}})
        )
        with pytest.raises(NotFoundError):
            await client.list_events('x')


async def test_list_events_429_rate_limit_raises_rate_limit_error(client):
    with respx.mock:
        respx.get(f'{GOOGLE_API_BASE}/calendars/x/events').mock(
            return_value=Response(
                429,
                json={
                    'error': {
                        'code': 429,
                        'message': 'Quota exceeded',
                        'errors': [{'reason': 'rateLimitExceeded'}],
                    }
                },
            )
        )
        with pytest.raises(RateLimitError, match='rateLimitExceeded'):
            await client.list_events('x')


async def test_list_events_403_user_rate_limit_raises_rate_limit_error(client):
    with respx.mock:
        respx.get(f'{GOOGLE_API_BASE}/calendars/x/events').mock(
            return_value=Response(
                403,
                json={
                    'error': {
                        'code': 403,
                        'message': 'User rate limit exceeded',
                        'errors': [{'reason': 'userRateLimitExceeded'}],
                    }
                },
            )
        )
        with pytest.raises(RateLimitError, match='userRateLimitExceeded'):
            await client.list_events('x')


async def test_list_events_403_other_reason_not_rate_limit(client):
    """Permission denied (not rate limit) -> generic GoogleApiError, not retryable."""
    with respx.mock:
        respx.get(f'{GOOGLE_API_BASE}/calendars/x/events').mock(
            return_value=Response(
                403,
                json={
                    'error': {
                        'code': 403,
                        'message': 'Forbidden',
                        'errors': [{'reason': 'forbidden'}],
                    }
                },
            )
        )
        with pytest.raises(GoogleApiError) as ei:
            await client.list_events('x')
        assert not isinstance(ei.value, RateLimitError)


async def test_list_events_sends_bearer_token(client):
    captured = {}

    def _capture(request):
        captured['auth'] = request.headers.get('authorization')
        return Response(200, json={'items': []})

    with respx.mock:
        respx.get(f'{GOOGLE_API_BASE}/calendars/x/events').mock(side_effect=_capture)
        await client.list_events('x')
    assert captured['auth'] == 'Bearer at-test'


# --- get_user_calendar_id ---


async def test_get_user_calendar_id_resolves_primary(client):
    with respx.mock:
        respx.get(f'{GOOGLE_API_BASE}/calendars/primary').mock(
            return_value=Response(200, json={'id': 'edmenendez@gmail.com', 'summary': 'Ed Menendez'})
        )
        result = await client.get_user_calendar_id()
    assert result == 'edmenendez@gmail.com'


async def test_get_user_calendar_id_404_raises_not_found_error(client):
    with respx.mock:
        respx.get(f'{GOOGLE_API_BASE}/calendars/primary').mock(
            return_value=Response(404, json={'error': {'message': 'Not Found'}})
        )
        with pytest.raises(NotFoundError):
            await client.get_user_calendar_id()


# --- design rule: no update_event method ---


def test_no_update_event_method_exists():
    """Hard rule from design: events.patch only, never events.update.
    Enforce at the type level so the method literally cannot be called."""
    client = GoogleCalendarClient(access_token='x')
    assert not hasattr(client, 'update_event')
