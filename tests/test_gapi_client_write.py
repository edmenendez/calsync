"""GoogleCalendarClient write-path tests (insert/patch/delete/watch/stop).

All HTTP traffic mocked with respx; no network.
"""

import pytest
import respx
from httpx import Response

from calsync.gapi.client import GOOGLE_API_BASE, GoogleCalendarClient
from calsync.gapi.errors import GoneError, NotFoundError, RateLimitError


@pytest.fixture
def client() -> GoogleCalendarClient:
    # retry_base_wait=0 keeps tests fast; retry behavior itself is tested in test_gapi_retry_throttle.
    return GoogleCalendarClient(access_token='at-test', retry_base_wait=0.0, retry_max_wait=0.0)


# --- insert_event ---


async def test_insert_event_basic(client):
    captured = {}

    def _capture(request):
        captured['body'] = request.content
        captured['url'] = str(request.url)
        return Response(200, json={'id': 'new-event-id', 'status': 'confirmed'})

    with respx.mock:
        respx.post(f'{GOOGLE_API_BASE}/calendars/x/events').mock(side_effect=_capture)
        result = await client.insert_event('x', {'summary': 'Busy'})

    assert result['id'] == 'new-event-id'
    assert b'"summary":"Busy"' in captured['body']


async def test_insert_event_passes_send_updates_none(client):
    """Mirror events should not send invitations even though they have no attendees."""
    captured = {}

    def _capture(request):
        captured['url'] = str(request.url)
        return Response(200, json={'id': 'e1'})

    with respx.mock:
        respx.post(f'{GOOGLE_API_BASE}/calendars/x/events').mock(side_effect=_capture)
        await client.insert_event('x', {'summary': 'Busy'})
    assert 'sendUpdates=none' in captured['url']


async def test_insert_event_includes_extended_properties_in_body(client):
    """The mirror_key + calsync_origin markers travel through the body unchanged."""
    captured = {}

    def _capture(request):
        captured['body'] = request.content.decode()
        return Response(200, json={'id': 'e1'})

    with respx.mock:
        respx.post(f'{GOOGLE_API_BASE}/calendars/x/events').mock(side_effect=_capture)
        await client.insert_event(
            'x',
            {
                'summary': 'Busy',
                'extendedProperties': {
                    'private': {
                        'calsync_origin': 'calsync',
                        'calsync_mirror_key': 'a1b2c3',
                    }
                },
            },
        )
    assert 'calsync_origin' in captured['body']
    assert 'calsync_mirror_key' in captured['body']
    assert 'a1b2c3' in captured['body']


async def test_insert_event_403_user_rate_limit_raises_rate_limit_error(client):
    with respx.mock:
        respx.post(f'{GOOGLE_API_BASE}/calendars/x/events').mock(
            return_value=Response(
                403,
                json={
                    'error': {
                        'message': 'User rate limit exceeded',
                        'errors': [{'reason': 'userRateLimitExceeded'}],
                    }
                },
            )
        )
        with pytest.raises(RateLimitError):
            await client.insert_event('x', {'summary': 'Busy'})


# --- patch_event ---


async def test_patch_event_uses_patch_method(client):
    """The hard rule: PATCH (partial), never PUT."""
    captured = {}

    def _capture(request):
        captured['method'] = request.method
        return Response(200, json={'id': 'e1', 'summary': 'updated'})

    with respx.mock:
        respx.patch(f'{GOOGLE_API_BASE}/calendars/x/events/e1').mock(side_effect=_capture)
        await client.patch_event('x', 'e1', {'summary': 'updated'})
    assert captured['method'] == 'PATCH'


async def test_patch_event_only_sends_provided_fields(client):
    """Patch semantics: body holds only the fields to change. Existing fields preserved."""
    captured = {}

    def _capture(request):
        captured['body'] = request.content.decode()
        return Response(200, json={'id': 'e1'})

    with respx.mock:
        respx.patch(f'{GOOGLE_API_BASE}/calendars/x/events/e1').mock(side_effect=_capture)
        await client.patch_event(
            'x',
            'e1',
            {'start': {'dateTime': '2026-05-04T10:00:00Z'}},
        )
    assert 'start' in captured['body']
    assert 'summary' not in captured['body']
    assert 'extendedProperties' not in captured['body']


async def test_patch_event_404_raises_not_found_error(client):
    """User manually deleted the mirror; caller should drop or recreate."""
    with respx.mock:
        respx.patch(f'{GOOGLE_API_BASE}/calendars/x/events/missing').mock(
            return_value=Response(404, json={'error': {'message': 'Not Found'}})
        )
        with pytest.raises(NotFoundError):
            await client.patch_event('x', 'missing', {'summary': 'x'})


async def test_patch_event_410_raises_gone_error(client):
    with respx.mock:
        respx.patch(f'{GOOGLE_API_BASE}/calendars/x/events/gone').mock(
            return_value=Response(410, json={'error': {'message': 'Resource has been deleted'}})
        )
        with pytest.raises(GoneError):
            await client.patch_event('x', 'gone', {'summary': 'x'})


# --- delete_event ---


async def test_delete_event_basic(client):
    """Google returns 204 No Content; we return None."""
    captured = {}

    def _capture(request):
        captured['method'] = request.method
        return Response(204)

    with respx.mock:
        respx.delete(f'{GOOGLE_API_BASE}/calendars/x/events/e1').mock(side_effect=_capture)
        result = await client.delete_event('x', 'e1')

    assert result is None
    assert captured['method'] == 'DELETE'


async def test_delete_event_404_raises_not_found_error(client):
    """Caller decides whether to treat as benign (cleanup paths typically do)."""
    with respx.mock:
        respx.delete(f'{GOOGLE_API_BASE}/calendars/x/events/missing').mock(
            return_value=Response(404, json={'error': {'message': 'Not Found'}})
        )
        with pytest.raises(NotFoundError):
            await client.delete_event('x', 'missing')


async def test_delete_event_410_raises_gone_error(client):
    with respx.mock:
        respx.delete(f'{GOOGLE_API_BASE}/calendars/x/events/gone').mock(
            return_value=Response(410, json={'error': {'message': 'Resource gone'}})
        )
        with pytest.raises(GoneError):
            await client.delete_event('x', 'gone')


# --- watch_events ---


async def test_watch_events_basic(client):
    captured = {}

    def _capture(request):
        captured['body'] = request.content.decode()
        return Response(
            200,
            json={
                'kind': 'api#channel',
                'id': 'channel-uuid-1',
                'resourceId': 'res-id-1',
                'resourceUri': 'https://...',
                'token': 'tok-1',
                'expiration': '1735689600000',
            },
        )

    with respx.mock:
        respx.post(f'{GOOGLE_API_BASE}/calendars/x/events/watch').mock(side_effect=_capture)
        result = await client.watch_events(
            'x',
            channel_id='channel-uuid-1',
            channel_token='tok-1',
            callback_url='https://calsync.menendez.com/webhook/google',
            ttl_seconds=86400 * 7,
        )

    assert result['resourceId'] == 'res-id-1'
    assert result['id'] == 'channel-uuid-1'
    assert '"id":"channel-uuid-1"' in captured['body']
    assert '"type":"web_hook"' in captured['body']
    assert '"address":"https://calsync.menendez.com/webhook/google"' in captured['body']
    assert '"token":"tok-1"' in captured['body']
    assert '"ttl":"604800"' in captured['body']


async def test_watch_events_passes_token(client):
    """The channel_token must reach Google so it can echo it back as X-Goog-Channel-Token."""
    captured = {}

    def _capture(request):
        captured['body'] = request.content.decode()
        return Response(200, json={'id': 'c', 'resourceId': 'r'})

    with respx.mock:
        respx.post(f'{GOOGLE_API_BASE}/calendars/x/events/watch').mock(side_effect=_capture)
        await client.watch_events(
            'x',
            channel_id='c',
            channel_token='secret-shared-token',
            callback_url='https://x/cb',
        )
    assert 'secret-shared-token' in captured['body']


# --- stop_channel ---


async def test_stop_channel_basic(client):
    captured = {}

    def _capture(request):
        captured['body'] = request.content.decode()
        return Response(204)

    with respx.mock:
        respx.post(f'{GOOGLE_API_BASE}/channels/stop').mock(side_effect=_capture)
        await client.stop_channel('channel-uuid-1', 'res-id-1')
    assert '"id":"channel-uuid-1"' in captured['body']
    assert '"resourceId":"res-id-1"' in captured['body']


async def test_stop_channel_404_raises_not_found_error(client):
    """Already stopped or expired - callers should treat as benign in renewal flows."""
    with respx.mock:
        respx.post(f'{GOOGLE_API_BASE}/channels/stop').mock(
            return_value=Response(404, json={'error': {'message': 'Channel not found'}})
        )
        with pytest.raises(NotFoundError):
            await client.stop_channel('c', 'r')
