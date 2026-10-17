"""Configuration from environment variables only (no secrets in code)."""

from __future__ import annotations

import os
from dataclasses import dataclass


class ConfigError(RuntimeError):
    """Raised when required configuration is missing or invalid."""


def _env(name: str, default: str | None = None) -> str | None:
    value = os.environ.get(name)
    if value is None or value.strip() == "":
        return default
    return value.strip()


def _env_float(name: str, default: float) -> float:
    raw = _env(name)
    if raw is None:
        return default
    try:
        return float(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} must be a number, got {raw!r}") from exc


def _env_int(name: str, default: int) -> int:
    raw = _env(name)
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} must be an integer, got {raw!r}") from exc


@dataclass(frozen=True)
class Settings:
    """Runtime settings.

    Environment variables:
      DATABASE_URL (required), S3_BUCKET, S3_ENDPOINT, S3_ACCESS_KEY, S3_SECRET_KEY, S3_REGION, MIGRATIONS_DIR,
      DB_POOL_MIN, DB_POOL_MAX, DB_TIMEOUT_S, HEALTH_TIMEOUT_S, MAX_UPLOAD_BYTES, LOG_LEVEL.
    """

    database_url: str
    s3_bucket: str = "inventory"
    s3_endpoint: str | None = None
    s3_access_key: str | None = None
    s3_secret_key: str | None = None
    s3_region: str = "us-east-1"
    migrations_dir: str | None = None
    db_pool_min: int = 1
    db_pool_max: int = 10
    db_timeout_s: float = 10.0
    health_timeout_s: float = 2.0
    max_upload_bytes: int = 20 * 1024 * 1024
    log_level: str = "INFO"

    @classmethod
    def from_env(cls) -> Settings:
        """Build settings from the process environment. ``DATABASE_URL`` is required."""
        database_url = _env("DATABASE_URL")
        if not database_url:
            raise ConfigError("DATABASE_URL environment variable is required")
        return cls(
            database_url=database_url,
            s3_bucket=_env("S3_BUCKET", "inventory") or "inventory",
            s3_endpoint=_env("S3_ENDPOINT"),
            s3_access_key=_env("S3_ACCESS_KEY"),
            s3_secret_key=_env("S3_SECRET_KEY"),
            s3_region=_env("S3_REGION", "us-east-1") or "us-east-1",
            migrations_dir=_env("MIGRATIONS_DIR"),
            db_pool_min=_env_int("DB_POOL_MIN", 1),
            db_pool_max=_env_int("DB_POOL_MAX", 10),
            db_timeout_s=_env_float("DB_TIMEOUT_S", 10.0),
            health_timeout_s=_env_float("HEALTH_TIMEOUT_S", 2.0),
            max_upload_bytes=_env_int("MAX_UPLOAD_BYTES", 20 * 1024 * 1024),
            log_level=_env("LOG_LEVEL", "INFO") or "INFO",
        )
