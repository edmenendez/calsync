from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class DateWindow(BaseModel):
    """Configures which events are eligible for mirroring based on start time."""

    mode: Literal['current_week', 'rolling', 'absolute', 'all'] = 'current_week'
    week_starts_on: Literal['monday', 'sunday'] = 'monday'
    timezone: str = 'America/New_York'
    lookback_days: int = 21
    lookahead_days: int = 180
    absolute_start: str | None = None
    absolute_end: str | None = None


class NosyncConfig(BaseModel):
    """Opt-out behavior for events whose title or description contains a token."""

    tokens: list[str] = Field(default_factory=lambda: ['[nosync]', '[private]'])
    scope: Literal['all', 'work'] = 'all'


class Settings(BaseSettings):
    """Process-level settings loaded from env vars and .env files.

    Sensitive values (Fernet key, HMAC secret, OAuth client secret) MUST
    come from the environment, never from the YAML config file.
    """

    model_config = SettingsConfigDict(
        env_prefix='CALSYNC_',
        env_file='.env',
        env_file_encoding='utf-8',
        extra='ignore',
    )

    env: Literal['dev', 'prod'] = 'dev'
    public_url: str = 'http://localhost:8000'

    fernet_key: str
    mirror_hmac_key: str
    admin_token: str

    google_client_id: str
    google_client_secret: str

    db_path: Path = Path('calsync.db')

    dry_run: bool = False
