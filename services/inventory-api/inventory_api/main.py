"""FastAPI application. Run with ``uvicorn inventory_api.main:app``."""

from __future__ import annotations

import logging
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from . import __version__, structure_memory
from .config import Settings
from .deps import AppContext
from .errors import install_handlers
from .logging_config import configure_logging
from .routers import catalog, health, ingest, items, media, review, search
from .storage import ObjectStore

logger = logging.getLogger(__name__)


def create_app(settings: Settings | None = None, object_store: ObjectStore | None = None) -> FastAPI:
    """Build the app. ``settings`` defaults to Settings.from_env() and ``object_store`` to an S3ObjectStore built
    from settings; both are resolved lazily so the app can start (and report 503) when the DB is unreachable."""
    ctx = AppContext(_settings=settings, _store=object_store)

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        if settings is None:
            configure_logging(os.environ.get("LOG_LEVEL", "INFO"))
        logger.info("inventory-api starting", extra={"version": __version__})
        try:
            yield
        finally:
            ctx.close()
            logger.info("inventory-api stopped")

    app = FastAPI(title="NASA Omni Inventory API", version=__version__, lifespan=lifespan)
    app.state.ctx = ctx
    install_handlers(app)
    for module in (health, items, review, catalog, search, media, ingest, structure_memory):
        app.include_router(module.router)
    return app


app = create_app()
