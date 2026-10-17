"""API error types and exception handlers."""

from __future__ import annotations

import logging

import psycopg
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from psycopg_pool import PoolTimeout

logger = logging.getLogger(__name__)


class ApiError(Exception):
    status_code = 400

    def __init__(self, detail: str):
        super().__init__(detail)
        self.detail = detail


class NotFound(ApiError):
    status_code = 404


class Conflict(ApiError):
    status_code = 409


class Unprocessable(ApiError):
    status_code = 422


class BadGateway(ApiError):
    status_code = 502


class ServiceUnavailable(ApiError):
    status_code = 503


def install_handlers(app: FastAPI) -> None:
    @app.exception_handler(ApiError)
    async def _api_error(_request: Request, exc: ApiError) -> JSONResponse:
        return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})

    @app.exception_handler(PoolTimeout)
    async def _pool_timeout(request: Request, _exc: PoolTimeout) -> JSONResponse:
        logger.error("database unavailable (pool timeout)", extra={"path": request.url.path})
        return JSONResponse(status_code=503, content={"detail": "database unavailable"})

    @app.exception_handler(psycopg.DataError)
    async def _bad_data(request: Request, exc: psycopg.DataError) -> JSONResponse:
        # Client-caused data errors (NUL bytes in text, jsonb escapes, numeric overflow, ...) are the caller's fault:
        # 422, never 500. The DB transaction was already rolled back by the context manager.
        message = getattr(getattr(exc, "diag", None), "message_primary", None) or str(exc)
        logger.warning("request rejected: invalid data", extra={"path": request.url.path, "error": message})
        return JSONResponse(status_code=422, content={"detail": f"invalid data: {message}"})

    @app.exception_handler(psycopg.OperationalError)
    async def _db_down(request: Request, exc: psycopg.OperationalError) -> JSONResponse:
        logger.error("database operational error", extra={"path": request.url.path, "error": str(exc)})
        return JSONResponse(status_code=503, content={"detail": "database unavailable"})
