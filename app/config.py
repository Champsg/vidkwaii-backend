"""Application settings loaded from environment variables / .env file."""

from __future__ import annotations

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    app_name: str = "VidKwaii API"
    debug: bool = False
    redis_url: str = "redis://localhost:6379/0"
    cache_ttl_seconds: int = 86400
    max_extract_seconds: int = 75
    max_merge_seconds: int = 900
    media_dir: str = "/tmp/mediahub_media"
    media_ttl_seconds: int = 1800
    cors_origins: list[str] = ["*"]
    log_level: str = "INFO"


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
