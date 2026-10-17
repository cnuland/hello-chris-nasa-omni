"""Lazy Postgres connection pool.

The pool is created on first use, so the app starts (and ``/healthz`` reports 503) while Postgres is unreachable.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Iterator
from contextlib import contextmanager

import psycopg
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

logger = logging.getLogger(__name__)


class Database:
    """Thin wrapper around a psycopg_pool ConnectionPool returning dict rows."""

    def __init__(self, url: str, *, min_size: int = 1, max_size: int = 10, timeout: float = 10.0):
        self.url = url
        self.min_size = min_size
        self.max_size = max(max_size, min_size, 1)
        self.timeout = timeout
        self._pool: ConnectionPool | None = None
        self._lock = threading.Lock()

    def _get_pool(self) -> ConnectionPool:
        if self._pool is None:
            with self._lock:
                if self._pool is None:
                    pool = ConnectionPool(
                        self.url,
                        min_size=self.min_size,
                        max_size=self.max_size,
                        kwargs={"row_factory": dict_row, "autocommit": False},
                        timeout=self.timeout,
                        open=False,
                        name="inventory-api",
                    )
                    pool.open(wait=False)
                    self._pool = pool
        return self._pool

    @contextmanager
    def connection(self, timeout: float | None = None) -> Iterator[psycopg.Connection]:
        """Borrow a connection. The transaction commits on normal exit and rolls back on an exception."""
        with self._get_pool().connection(timeout=timeout) as conn:
            yield conn

    def ping(self, timeout: float) -> bool:
        """Return True when ``SELECT 1`` succeeds within ``timeout`` seconds."""
        try:
            with self.connection(timeout=timeout) as conn:
                conn.execute("SELECT 1").fetchone()
            return True
        except Exception as exc:  # noqa: BLE001 - any failure means "not healthy"
            logger.warning("database ping failed", extra={"error": type(exc).__name__})
            return False

    def close(self) -> None:
        with self._lock:
            if self._pool is not None:
                self._pool.close(timeout=5)
                self._pool = None
