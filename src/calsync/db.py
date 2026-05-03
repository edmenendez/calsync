"""SQLite connection helpers.

Single-writer model: per-calendar serialization happens at the application
layer (asyncio locks). Connections are short-lived; we open one per
operation rather than maintain a long-lived shared connection.
"""

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from importlib import resources
from pathlib import Path


def _load_schema() -> str:
    return resources.files('calsync').joinpath('schema.sql').read_text(encoding='utf-8')


def init_db(path: Path) -> None:
    """Create tables, indexes, and PRAGMAs if they don't already exist."""
    path.parent.mkdir(parents=True, exist_ok=True)
    schema = _load_schema()
    with sqlite3.connect(path) as conn:
        conn.executescript(schema)


@contextmanager
def connect(path: Path) -> Iterator[sqlite3.Connection]:
    """Open a connection with row factory and FK enforcement enabled."""
    conn = sqlite3.connect(path, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA foreign_keys = ON')
    try:
        yield conn
    finally:
        conn.close()
