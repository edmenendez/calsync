"""Async Google Calendar REST client.

Thin wrapper around httpx with typed error mapping. Covers the endpoints
calsync needs: events (list/insert/patch/delete/watch), calendars (get),
channels (stop). NO `update_event` method - the design forbids
`events.update` (PUT) since it can strip extended properties.

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

    async def _post(self, url: str, *, json: dict | None = None, params: dict | None = None) -> dict:
        headers = {**self._headers, 'Content-Type': 'application/json'}
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            r = await client.post(url, json=json, params=params or {}, headers=headers)
        if r.is_success:
            return r.json() if r.content else {}
        raise _classify_error(r)

    async def _patch(self, url: str, *, json: dict) -> dict:
        headers = {**self._headers, 'Content-Type': 'application/json'}
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            r = await client.patch(url, json=json, headers=headers)
        if r.is_success:
            return r.json()
        raise _classify_error(r)

    async def _delete(self, url: str) -> None:
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            r = await client.delete(url, headers=self._headers)
        if r.is_success:
            return
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

    async def insert_event(self, calendar_id: str, body: dict) -> dict:
        """Wrapper for events.insert. Returns the created event dict.

        Used to create mirror events. The body should already contain the
        calsync_origin and calsync_mirror_key extended properties. Inserts
        from this client deliberately do NOT send invitations to attendees;
        mirror events have no attendees anyway, but pass `sendUpdates=none`
        as belt-and-suspenders.
        """
        url = f'{GOOGLE_API_BASE}/calendars/{calendar_id}/events'
        return await self._post(url, json=body, params={'sendUpdates': 'none'})

    async def patch_event(self, calendar_id: str, event_id: str, body: dict) -> dict:
        """Wrapper for events.patch. PARTIAL update - preserves extendedProperties.

        Hard rule from the design plan: only patch_event. Never update_event
        (PUT), which would replace the resource and strip our calsync_*
        extended properties unless they were explicitly echoed back.

        Returns the updated event dict.
        """
        url = f'{GOOGLE_API_BASE}/calendars/{calendar_id}/events/{event_id}'
        return await self._patch(url, json=body)

    async def delete_event(self, calendar_id: str, event_id: str) -> None:
        """Wrapper for events.delete. Returns None on success.

        Raises NotFoundError on 404 (already deleted), GoneError on 410
        (resource permanently gone). Callers should treat both as success
        for cleanup paths.
        """
        url = f'{GOOGLE_API_BASE}/calendars/{calendar_id}/events/{event_id}'
        await self._delete(url)

    async def watch_events(
        self,
        calendar_id: str,
        *,
        channel_id: str,
        channel_token: str,
        callback_url: str,
        ttl_seconds: int = 7 * 86400,
    ) -> dict:
        """Register a push-notification channel via events.watch.

        We choose the channel_id (UUIDv4 expected). Google returns a
        resourceId that we MUST store and validate on every incoming
        webhook (tuple match per design).

        Returns the watch response dict, which includes resourceId and
        expiration (epoch ms) among other fields.
        """
        url = f'{GOOGLE_API_BASE}/calendars/{calendar_id}/events/watch'
        body = {
            'id': channel_id,
            'type': 'web_hook',
            'address': callback_url,
            'token': channel_token,
            'params': {'ttl': str(ttl_seconds)},
        }
        return await self._post(url, json=body)

    async def stop_channel(self, channel_id: str, resource_id: str) -> None:
        """Stop a previously-registered watch channel via channels.stop.

        Best-effort during renewal: if Google returns NotFoundError or
        GoneError, the channel is already stopped/expired and that's fine.
        """
        url = f'{GOOGLE_API_BASE}/channels/stop'
        await self._post(url, json={'id': channel_id, 'resourceId': resource_id})
