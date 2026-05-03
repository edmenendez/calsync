"""Rate-limit retry (tenacity) and per-account semaphore (asyncio) tests."""

import asyncio

import pytest
import respx
from httpx import Response

from calsync.gapi.client import GOOGLE_API_BASE, GoogleCalendarClient
from calsync.gapi.errors import GoogleApiError, RateLimitError
from calsync.gapi.throttle import get_account_semaphore, reset


@pytest.fixture(autouse=True)
def _reset_semaphores():
    reset()
    yield
    reset()


def _fast_client(**overrides) -> GoogleCalendarClient:
    """Client with retry waits at zero so tests don't actually sleep."""
    defaults = {
        'access_token': 'at',
        'retry_base_wait': 0.0,
        'retry_max_wait': 0.0,
    }
    return GoogleCalendarClient(**{**defaults, **overrides})


# --- retry behavior ---


async def test_retries_on_rate_limit_and_eventually_succeeds():
    client = _fast_client()
    sequence = [
        Response(429, json={'error': {'message': 'quota', 'errors': [{'reason': 'rateLimitExceeded'}]}}),
        Response(429, json={'error': {'message': 'quota', 'errors': [{'reason': 'rateLimitExceeded'}]}}),
        Response(200, json={'items': [{'id': 'e1'}]}),
    ]
    with respx.mock:
        respx.get(f'{GOOGLE_API_BASE}/calendars/x/events').mock(side_effect=sequence)
        result = await client.list_events('x')
    assert result['items'] == [{'id': 'e1'}]


async def test_raises_after_max_attempts():
    client = _fast_client(retry_max_attempts=3)
    response = Response(429, json={'error': {'message': 'quota', 'errors': [{'reason': 'rateLimitExceeded'}]}})
    with respx.mock:
        route = respx.get(f'{GOOGLE_API_BASE}/calendars/x/events').mock(return_value=response)
        with pytest.raises(RateLimitError):
            await client.list_events('x')
    assert route.call_count == 3


async def test_403_user_rate_limit_is_retried():
    client = _fast_client()
    sequence = [
        Response(403, json={'error': {'errors': [{'reason': 'userRateLimitExceeded'}]}}),
        Response(200, json={'items': []}),
    ]
    with respx.mock:
        respx.get(f'{GOOGLE_API_BASE}/calendars/x/events').mock(side_effect=sequence)
        result = await client.list_events('x')
    assert result['items'] == []


async def test_403_other_reason_is_not_retried():
    """Permission denial (forbidden) must fail fast, not retry."""
    client = _fast_client()
    response = Response(403, json={'error': {'errors': [{'reason': 'forbidden'}]}})
    with respx.mock:
        route = respx.get(f'{GOOGLE_API_BASE}/calendars/x/events').mock(return_value=response)
        with pytest.raises(GoogleApiError):
            await client.list_events('x')
    assert route.call_count == 1


async def test_404_is_not_retried():
    """Non-rate-limit failures (here 404) MUST fail on first attempt, not retry."""
    client = _fast_client()
    response = Response(404, json={'error': {'message': 'not found'}})
    with respx.mock:
        route = respx.delete(f'{GOOGLE_API_BASE}/calendars/x/events/missing').mock(return_value=response)
        with pytest.raises(GoogleApiError):
            await client.delete_event('x', 'missing')
    assert route.call_count == 1


# --- semaphore behavior ---


async def test_semaphore_serializes_concurrent_requests():
    """With Semaphore(2), at most 2 concurrent requests are in flight."""
    sem = asyncio.Semaphore(2)
    client = _fast_client(semaphore=sem)

    in_flight = 0
    peak = 0
    lock = asyncio.Lock()

    async def _capture(_request):
        nonlocal in_flight, peak
        async with lock:
            in_flight += 1
            peak = max(peak, in_flight)
        await asyncio.sleep(0.01)
        async with lock:
            in_flight -= 1
        return Response(200, json={'items': []})

    with respx.mock:
        respx.get(f'{GOOGLE_API_BASE}/calendars/x/events').mock(side_effect=_capture)
        await asyncio.gather(*[client.list_events('x') for _ in range(8)])

    assert peak <= 2


async def test_get_account_semaphore_returns_same_object_per_account():
    s1 = get_account_semaphore(1)
    s2 = get_account_semaphore(1)
    assert s1 is s2


async def test_get_account_semaphore_different_per_account():
    s1 = get_account_semaphore(1)
    s2 = get_account_semaphore(2)
    assert s1 is not s2


async def test_get_account_semaphore_default_concurrency():
    """Per the design: 10 concurrent per account, leaving headroom under the 600/min limit."""
    sem = get_account_semaphore(99)
    # Acquiring 10 consecutively should be possible without blocking.
    for _ in range(10):
        await asyncio.wait_for(sem.acquire(), timeout=0.01)
    # 11th would block; verify by trying with timeout.
    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(sem.acquire(), timeout=0.01)
    # Release for cleanup
    for _ in range(10):
        sem.release()
