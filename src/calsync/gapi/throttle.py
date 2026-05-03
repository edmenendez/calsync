"""Per-account semaphore registry.

Caps concurrent in-flight Calendar API requests per Google account.
Single-worker design from the plan: this in-memory dict is sufficient.
For multi-worker, replace with a DB-backed advisory lock.
"""

import asyncio

from calsync.gapi.client import DEFAULT_PER_ACCOUNT_CONCURRENCY

_semaphores: dict[int, asyncio.Semaphore] = {}


def get_account_semaphore(account_id: int, *, concurrency: int = DEFAULT_PER_ACCOUNT_CONCURRENCY) -> asyncio.Semaphore:
    """Return the semaphore for an account, creating it on first call."""
    sem = _semaphores.get(account_id)
    if sem is None:
        sem = asyncio.Semaphore(concurrency)
        _semaphores[account_id] = sem
    return sem


def reset() -> None:
    """Drop all semaphores. Used in tests for isolation."""
    _semaphores.clear()
