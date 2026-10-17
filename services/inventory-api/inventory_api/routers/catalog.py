"""AC-API-14 catalog, locations and events; AC-API-23 GET /stock."""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Query
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse

from ..catalog_ops import CATALOG_COLUMNS, insert_catalog_item
from ..deps import Ctx, operator_actor
from ..repo import EVENT_COLUMNS, LOCATION_SUBTREE_SQL, serialize_event, write_audit
from ..schemas import CatalogItemIn
from ..storage import uri_to_url

logger = logging.getLogger(__name__)
router = APIRouter(tags=["catalog"])


def _serialize_catalog(row: dict[str, Any], bucket: str) -> dict[str, Any]:
    return {
        "catalog_id": row["catalog_id"],
        "class_name": row["class_name"],
        "display_name": row["display_name"],
        "synonyms": list(row["synonyms"] or []),
        "category": row["category"],
        "is_fungible": row["is_fungible"],
        "expected_count": row["expected_count"],
        "home_location": row["home_location"],
        "reference_image_url": uri_to_url(row["reference_image_uri"], bucket),
        "created_at": row["created_at"],
    }


@router.get("/catalog")
def list_catalog(ctx: Ctx) -> list[dict[str, Any]]:
    with ctx.db.connection() as conn:
        rows = conn.execute(f"SELECT {CATALOG_COLUMNS} FROM catalog_item ORDER BY catalog_id").fetchall()
    return [_serialize_catalog(r, ctx.bucket) for r in rows]


@router.post("/catalog", status_code=201)
def create_catalog_item(body: CatalogItemIn, ctx: Ctx) -> JSONResponse:
    actor = operator_actor(body.operator)
    with ctx.db.connection() as conn, conn.transaction():
        row = insert_catalog_item(conn, body)
        write_audit(
            conn,
            "create",
            [],
            actor,
            {
                "catalog_item": {k: v for k, v in row.items() if k != "created_at"},
            },
        )
    logger.info("catalog item created", extra={"catalog_id": body.catalog_id, "actor": actor})
    return JSONResponse(jsonable_encoder(_serialize_catalog(row, ctx.bucket)), status_code=201)


@router.get("/locations")
def list_locations(ctx: Ctx) -> list[dict[str, Any]]:
    with ctx.db.connection() as conn:
        return conn.execute(
            "SELECT location_id, parent_id, kind, display_name, no_capture FROM location ORDER BY location_id"
        ).fetchall()


@router.get("/events")
def list_events(
    ctx: Ctx,
    entity_id: UUID | None = None,
    type: str | None = Query(None, max_length=64),
    after: datetime | None = None,
    before: datetime | None = None,
    limit: int = Query(100, ge=1, le=1000),
) -> list[dict[str, Any]]:
    where: list[str] = []
    params: list[Any] = []
    if entity_id is not None:
        where.append("entity_id = %s")
        params.append(entity_id)
    if type:
        where.append("event_type = %s")
        params.append(type)
    if after is not None:
        where.append("occurred_at >= %s")
        params.append(after)
    if before is not None:
        where.append("occurred_at <= %s")
        params.append(before)
    sql = f"SELECT {EVENT_COLUMNS} FROM event"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY occurred_at DESC, created_at DESC LIMIT %s"
    params.append(limit)
    with ctx.db.connection() as conn:
        rows = conn.execute(sql, params).fetchall()
    return [serialize_event(r, ctx.bucket, uri_to_url) for r in rows]


@router.get("/stock")
def list_stock(
    ctx: Ctx,
    catalog_id: str | None = None,
    location: str | None = None,
) -> list[dict[str, Any]]:
    where: list[str] = []
    params: list[Any] = []
    if catalog_id:
        where.append("s.catalog_id = %s")
        params.append(catalog_id)
    if location:
        where.append(f"s.location_id IN ({LOCATION_SUBTREE_SQL})")
        params.append(location)
    sql = """SELECT s.catalog_id, c.display_name AS catalog_name, s.location_id, l.display_name AS location_name,
                    s.est_count, s.last_seen_at
               FROM stock s
               JOIN catalog_item c ON c.catalog_id = s.catalog_id
               JOIN location l ON l.location_id = s.location_id"""
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY s.catalog_id, s.location_id"
    with ctx.db.connection() as conn:
        return conn.execute(sql, params).fetchall()
