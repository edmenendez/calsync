import pytest
from pydantic import ValidationError

from calsync.config import DateWindow, NosyncConfig, Settings


def test_settings_loads_from_env(monkeypatch):
    monkeypatch.setenv('CALSYNC_FERNET_KEY', 'k1')
    monkeypatch.setenv('CALSYNC_MIRROR_HMAC_KEY', 'k2')
    monkeypatch.setenv('CALSYNC_ADMIN_TOKEN', 't')
    monkeypatch.setenv('CALSYNC_GOOGLE_CLIENT_ID', 'cid')
    monkeypatch.setenv('CALSYNC_GOOGLE_CLIENT_SECRET', 'csec')

    s = Settings(_env_file=None)
    assert s.fernet_key == 'k1'
    assert s.mirror_hmac_key == 'k2'
    assert s.admin_token == 't'
    assert s.google_client_id == 'cid'
    assert s.google_client_secret == 'csec'
    assert s.env == 'dev'
    assert s.dry_run is False


def test_settings_missing_required_raises(monkeypatch):
    for k in (
        'CALSYNC_FERNET_KEY',
        'CALSYNC_MIRROR_HMAC_KEY',
        'CALSYNC_ADMIN_TOKEN',
        'CALSYNC_GOOGLE_CLIENT_ID',
        'CALSYNC_GOOGLE_CLIENT_SECRET',
    ):
        monkeypatch.delenv(k, raising=False)
    with pytest.raises(ValidationError):
        Settings(_env_file=None)


def test_date_window_defaults():
    w = DateWindow()
    assert w.mode == 'current_week'
    assert w.week_starts_on == 'monday'
    assert w.lookback_days == 21
    assert w.lookahead_days == 180


def test_nosync_defaults():
    n = NosyncConfig()
    assert '[nosync]' in n.tokens
    assert n.scope == 'all'
