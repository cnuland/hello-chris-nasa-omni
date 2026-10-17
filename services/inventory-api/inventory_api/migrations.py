"""Migrations runner: applies contracts/db/*.sql in lexical order (001_init.sql, 002_seed.sql, ...).

The contract SQL is written to be idempotent (``IF NOT EXISTS`` / ``ON CONFLICT DO NOTHING``), so every file is
applied on every run, each in its own transaction, under a session advisory lock so that concurrently starting
replicas do not race.

Usage: ``python -m inventory_api.migrations`` (reads DATABASE_URL and MIGRATIONS_DIR).
"""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

import psycopg

from .logging_config import configure_logging

logger = logging.getLogger(__name__)

ADVISORY_LOCK_ID = 0x1A5A_0001
DEFAULT_DIRS = (Path("/opt/app-root/src/migrations"), Path(__file__).resolve().parents[3] / "contracts" / "db")


def migration_files(migrations_dir: str | Path) -> list[Path]:
    """Return the *.sql files in apply order."""
    directory = Path(migrations_dir)
    if not directory.is_dir():
        raise FileNotFoundError(f"migrations directory not found: {directory}")
    return sorted((p for p in directory.glob("*.sql") if p.is_file()), key=lambda p: p.name)


def apply_migrations(database_url: str, migrations_dir: str | Path) -> list[str]:
    """Apply every migration in order (idempotent); return the applied file names in order."""
    files = migration_files(migrations_dir)
    applied: list[str] = []
    with psycopg.connect(database_url, autocommit=True) as conn:
        conn.execute("SELECT pg_advisory_lock(%s)", (ADVISORY_LOCK_ID,))
        try:
            for path in files:
                with conn.transaction():
                    conn.execute(path.read_text(encoding="utf-8"))
                logger.info("migration applied", extra={"file": path.name})
                applied.append(path.name)
        finally:
            conn.execute("SELECT pg_advisory_unlock(%s)", (ADVISORY_LOCK_ID,))
    return applied


def _resolve_dir() -> Path:
    env_dir = os.environ.get("MIGRATIONS_DIR")
    if env_dir:
        return Path(env_dir)
    for candidate in DEFAULT_DIRS:
        if candidate.is_dir():
            return candidate
    raise FileNotFoundError("MIGRATIONS_DIR is not set and no default migrations directory exists")


def main() -> None:  # pragma: no cover - thin CLI wrapper
    configure_logging(os.environ.get("LOG_LEVEL", "INFO"))
    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        logger.error("DATABASE_URL environment variable is required")
        sys.exit(2)
    applied = apply_migrations(database_url, _resolve_dir())
    logger.info("migrations complete", extra={"applied": applied})


if __name__ == "__main__":  # pragma: no cover
    main()
