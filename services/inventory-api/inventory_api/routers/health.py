"""AC-API-1: liveness + DB check."""

from __future__ import annotations

from fastapi import APIRouter
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse

from ..deps import Ctx

router = APIRouter(tags=["health"])


@router.get("/healthz")
async def healthz(ctx: Ctx) -> JSONResponse:
    try:
        timeout = ctx.settings.health_timeout_s
        ok = await run_in_threadpool(ctx.db.ping, timeout)
    except Exception:  # noqa: BLE001 - misconfiguration also means unhealthy
        ok = False
    if ok:
        return JSONResponse({"status": "ok", "db": True})
    return JSONResponse({"status": "degraded", "db": False}, status_code=503)
