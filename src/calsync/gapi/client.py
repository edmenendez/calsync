"""Async Google Calendar REST client.

Thin wrapper around httpx with typed error mapping. Read paths only in
this module's first incarnation; write paths (insert/patch/delete/watch)
arrive in a follow-up commit.

Auth is handed in: callers construct a `GoogleCalendarClient` after they
already have a valid access_token via `ensure_access_token()` from
`gapi.auth`. The client itself does NOT refresh tokens; that's a layer up.
"""

from typing import Any

import httpx

from calsync.gapi.errors import (
    GoneError,
    GoogleApiError,
    NotFoundError,
    RateLimitError,
)

GOOGLE_API_BASE = 'https://www.googleapis.com/calendar/v3'

RATE_LIMIT_REASONS = frozenset({'rateLimitExceeded', 'userRateLimitExceeded'})


def _classify_error(response: httpx.Response) -> GoogleApiError:
    """Map a non-2xx response to a typed exception."""
    status = response.status_code
    reason = ''
    message = ''
    try:
        body = response.json()
        err = body.get('error', {})
        message = err.get('message', '')
        errors = err.get('errors') or []
        if errors:
            reason = errors[0].get('reason', '')
    except (ValueError, KeyError):
        message = response.text[:200]

    if status == 410:
        return GoneError(f'410 Gone: {message}')
    if status == 404:
        return NotFoundError(f'404 Not Found: {message}')
    if status in (403, 429) and reason in RATE_LIMIT_REASONS:
        return RateLimitError(f'{status} {reason}: {message}')
    return GoogleApiError(f'{status} {reason or "error"}: {message}')


class GoogleCalendarClient:
    """REST client for one access token. Construct fresh per request batch.

    The same client is reusable across multiple calls until the access
    token expires; callers are expected to construct a new one (with a
    refreshed token) when needed.
    """

    def __init__(self, access_token: str, *, timeout: float = 10.0):
        self._access_token = access_token
        self._timeout = timeout

    @property
    def _headers(self) -> dict[str, str]:
        return {'Authorization': f'Bearer {self._access_token}', 'Accept': 'application/json'}

    async def _get(self, url: str, params: dict[str, Any]) -> dict:
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            r = await client.get(url, params=params, headers=self._headers)
        if r.is_success:
            return r.json()
        raise _classify_error(r)

    async def list_events(
        self,
        calendar_id: str,
        *,
        sync_token: str | None = None,
        time_min: str | None = None,
        time_max: str | None = None,
        private_extended_property: list[str] | None = None,
        page_token: str | None = None,
        show_deleted: bool = True,
        single_events: bool = False,
        max_results: int = 250,
    ) -> dict:
        """Wrapper for events.list.

        Returns the raw response dict (with `items`, `nextPageToken` or
        `nextSyncToken`).

        `show_deleted=True` is the default per the design plan: tombstones
        for deleted source events MUST appear in syncToken deltas so we
        can propagate deletions to mirrors.

        Mutually exclusive parameters per Google's API:
        - `sync_token` cannot be combined with `time_min`/`time_max`
        - `single_events=False` keeps recurring masters intact (preferred
          for our v1 sync logic which handles recurrence at the master
          level).
        """
        params: dict[str, Any] = {
            'showDeleted': 'true' if show_deleted else 'false',
            'singleEvents': 'true' if single_events else 'false',
            'maxResults': max_results,
        }
        if sync_token:
            params['syncToken'] = sync_token
        if time_min:
            params['timeMin'] = time_min
        if time_max:
            params['timeMax'] = time_max
        if page_token:
            params['pageToken'] = page_token
        if private_extended_property:
            # Google accepts repeated query params; httpx serializes lists this way.
            params['privateExtendedProperty'] = list(private_extended_property)

        url = f'{GOOGLE_API_BASE}/calendars/{calendar_id}/events'
        return await self._get(url, params)

    async def get_user_calendar_id(self) -> str:
        """Resolve `primary` to the canonical Google calendar ID for this account.

        Used right after OAuth so we can store the stable identifier in
        accounts.google_calendar_id and feed it into HMAC mirror_key
        derivation. The primary calendar's ID is the user's email address
        for individual accounts, or a Workspace-resource string for some
        org configurations.
        """
        url = f'{GOOGLE_API_BASE}/calendars/primary'
        data = await self._get(url, {})
        return data['id']
