"""Application context and FastAPI dependencies."""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import Annotated

from fastapi import Depends, Request

from .config import Settings
from .db import Database
from .storage import ObjectStore, S3ObjectStore

DEFAULT_OPERATOR_ACTOR = "unattributed-operator"


def operator_actor(operator: str | None) -> str:
    """Actor for operator-initiated writes; never empty and never 'pipeline' (AC-API-21)."""
    name = (operator or "").strip()
    if not name or name == "pipeline":
        return DEFAULT_OPERATOR_ACTOR
    return name[:200]


@dataclass
class AppContext:
    """Lazily resolved settings, DB pool and object store (so the app can start while dependencies are down)."""

    _settings: Settings | None = None
    _store: ObjectStore | None = None
    _db: Database | None = None
    _lock: threading.Lock = field(default_factory=threading.Lock)

    @property
    def settings(self) -> Settings:
        if self._settings is None:
            with self._lock:
                if self._settings is None:
                    self._settings = Settings.from_env()
        return self._settings

    @property
    def db(self) -> Database:
        if self._db is None:
            settings = self.settings
            with self._lock:
                if self._db is None:
                    self._db = Database(
                        settings.database_url,
                        min_size=settings.db_pool_min,
                        max_size=settings.db_pool_max,
                        timeout=settings.db_timeout_s,
                    )
        return self._db

    @property
    def store(self) -> ObjectStore:
        if self._store is None:
            settings = self.settings
            with self._lock:
                if self._store is None:
                    self._store = S3ObjectStore.from_settings(settings)
        return self._store

    @property
    def bucket(self) -> str:
        return self.store.bucket

    def close(self) -> None:
        if self._db is not None:
            self._db.close()


def get_ctx(request: Request) -> AppContext:
    return request.app.state.ctx


Ctx = Annotated[AppContext, Depends(get_ctx)]
