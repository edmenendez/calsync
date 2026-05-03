"""Per-calendar processing locks.

Each calendar has at most one in-flight delta-pull at a time. This is
the design's "per-calendar lock" defense; in single-uvicorn-worker mode
an asyncio.Lock per calendar is sufficient.
"""

import asyncio

_locks: dict[int, asyncio.Lock] = {}


def get_calendar_lock(calendar_id: int) -> asyncio.Lock:
    lock = _locks.get(calendar_id)
    if lock is None:
        lock = asyncio.Lock()
        _locks[calendar_id] = lock
    return lock


def reset() -> None:
    """Drop all locks. Test-only."""
    _locks.clear()
