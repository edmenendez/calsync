"""Typed exceptions for Google Calendar API failures.

Callers catch these by type rather than inspecting status codes:

    try:
        await client.list_events(...)
    except GoneError:
        # syncToken expired; do a full re-list
    except RateLimitError:
        # tenacity retry already exhausted; back off harder

`RateLimitError` is the only one tenacity retries by default - the others
indicate state that won't change with a retry.
"""


class GoogleApiError(Exception):
    """Base class for all Google Calendar API errors."""


class RateLimitError(GoogleApiError):
    """403 or 429 with rateLimitExceeded / userRateLimitExceeded reason. Retryable."""


class GoneError(GoogleApiError):
    """410 - syncToken or watch channel resource gone. Caller should re-list or re-watch."""


class NotFoundError(GoogleApiError):
    """404 - event or calendar not found. Common when a mirror was manually deleted."""


class NeedsReauthError(GoogleApiError):
    """OAuth refresh token is no longer valid (invalid_grant). User must re-authorize."""
