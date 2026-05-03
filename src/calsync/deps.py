"""Shared FastAPI dependencies."""

from functools import lru_cache

from calsync.config import Settings


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
