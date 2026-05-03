"""Shared test fixtures."""

from pathlib import Path

import pytest

from calsync.config import Settings
from calsync.crypto import generate_fernet_key, generate_hmac_key
from calsync.db import init_db
from calsync.deps import get_settings


@pytest.fixture
def settings(tmp_path: Path, monkeypatch) -> Settings:
    """A fully-configured Settings instance pointing at a temp DB."""
    db = tmp_path / 'calsync.db'
    init_db(db)

    monkeypatch.setenv('CALSYNC_FERNET_KEY', generate_fernet_key())
    monkeypatch.setenv('CALSYNC_MIRROR_HMAC_KEY', generate_hmac_key())
    monkeypatch.setenv('CALSYNC_ADMIN_TOKEN', 'test-admin-token')
    monkeypatch.setenv('CALSYNC_GOOGLE_CLIENT_ID', 'test-client-id')
    monkeypatch.setenv('CALSYNC_GOOGLE_CLIENT_SECRET', 'test-client-secret')
    monkeypatch.setenv('CALSYNC_DB_PATH', str(db))
    monkeypatch.setenv('CALSYNC_PUBLIC_URL', 'http://localhost:8000')

    s = Settings(_env_file=None)
    get_settings.cache_clear()
    return s


@pytest.fixture
def app_client(settings: Settings):
    """A FastAPI TestClient bound to the temp-DB settings."""
    from fastapi.testclient import TestClient

    from calsync.main import app

    def _override():
        return settings

    app.dependency_overrides[get_settings] = _override
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.clear()
