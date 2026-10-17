"""AC-API-20: proxy objects from the object store, with path-traversal protection."""

from __future__ import annotations

import logging

from fastapi import APIRouter
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse, Response, StreamingResponse

from ..deps import Ctx
from ..storage import InvalidKey, ObjectNotFound, ObjectStoreError, ObjectStoreUnavailable, validate_key

logger = logging.getLogger(__name__)
router = APIRouter(tags=["media"])

_SAFE_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "Content-Security-Policy": "default-src 'none'; sandbox",
    "Cache-Control": "private, max-age=300",
}


@router.get("/media/{key:path}")
async def get_media(key: str, ctx: Ctx) -> Response:
    # ``key`` is already percent-decoded by the router, so encoded '..', '/' and '\' are caught here.
    try:
        validate_key(key)
        obj = await run_in_threadpool(ctx.store.get, key)
    except InvalidKey:
        logger.warning("rejected unsafe media key", extra={"key": key[:200]})
        return JSONResponse({"detail": "invalid key"}, status_code=400)
    except ObjectNotFound:
        return JSONResponse({"detail": "not found"}, status_code=404)
    except ObjectStoreUnavailable:
        return JSONResponse({"detail": "object store unavailable"}, status_code=503)
    except ObjectStoreError:
        return JSONResponse({"detail": "object store error"}, status_code=502)
    headers = dict(_SAFE_HEADERS)
    if obj.content_length is not None:
        headers["Content-Length"] = str(obj.content_length)
    # The sync chunk iterator is driven from the threadpool by StreamingResponse; memory stays at one chunk.
    return StreamingResponse(obj.chunks, media_type=obj.content_type, headers=headers)
