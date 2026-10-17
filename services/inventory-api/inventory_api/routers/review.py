"""AC-API-12 review queue and AC-API-13 resolve actions."""

from __future__ import annotations

import logging
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Query

from ..catalog_ops import insert_catalog_item
from ..deps import Ctx, operator_actor
from ..errors import Conflict, NotFound, Unprocessable
from ..merging import merge_entities
from ..repo import fetch_items, jsonb, write_audit
from ..schemas import ResolveRequest, ReviewKind, ReviewStatus
from ..storage import uri_to_url

logger = logging.getLogger(__name__)
router = APIRouter(tags=["review"])

REVIEW_COLUMNS = """review_id, kind, entity_id, candidate_ids, image_uri, description, observed_at, location_id,
                    model_guess, probabilities, status, resolution, resolved_by, resolved_at, created_at"""


def _serialize_review(row: dict[str, Any], candidates: list[dict[str, Any]], bucket: str) -> dict[str, Any]:
    return {
        "review_id": row["review_id"],
        "kind": row["kind"],
        "entity_id": row["entity_id"],
        "candidate_ids": [str(c) for c in row["candidate_ids"] or []],
        "candidates": candidates,
        # Only URLs /media can serve; arbitrary stored strings never reach the dashboard.
        "image_url": uri_to_url(row["image_uri"], bucket),
        "description": row["description"],
        "observed_at": row["observed_at"],
        "location_id": row["location_id"],
        "model_guess": row["model_guess"],
        "probabilities": row["probabilities"] or {},
        "status": row["status"],
        "resolution": row["resolution"],
        "resolved_by": row["resolved_by"],
        "resolved_at": row["resolved_at"],
        "created_at": row["created_at"],
    }


def _expand(conn, rows: list[dict[str, Any]], bucket: str) -> list[dict[str, Any]]:
    all_ids = list(dict.fromkeys(str(c) for r in rows for c in (r["candidate_ids"] or [])))
    items = {str(i["entity_id"]): i for i in fetch_items(conn, all_ids, bucket, uri_to_url)}
    return [
        _serialize_review(r, [items[str(c)] for c in (r["candidate_ids"] or []) if str(c) in items], bucket)
        for r in rows
    ]


@router.get("/review")
def list_reviews(
    ctx: Ctx,
    status: ReviewStatus = "open",
    kind: ReviewKind | None = None,
    limit: int = Query(50, ge=1, le=500),
) -> list[dict[str, Any]]:
    sql = f"SELECT {REVIEW_COLUMNS} FROM review_item WHERE status = %s"
    params: list[Any] = [status]
    if kind:
        sql += " AND kind = %s"
        params.append(kind)
    sql += " ORDER BY created_at DESC, observed_at DESC, review_id LIMIT %s"
    params.append(limit)
    with ctx.db.connection() as conn:
        rows = conn.execute(sql, params).fetchall()
        return _expand(conn, rows, ctx.bucket)


@router.post("/review/{review_id}/resolve")
def resolve_review(review_id: UUID, body: ResolveRequest, ctx: Ctx) -> dict[str, Any]:
    actor = operator_actor(body.operator)
    with ctx.db.connection() as conn:
        with conn.transaction():
            review = conn.execute(
                f"SELECT {REVIEW_COLUMNS} FROM review_item WHERE review_id = %s FOR UPDATE", (review_id,)
            ).fetchone()
            if review is None:
                raise NotFound(f"review {review_id} not found")
            if review["status"] != "open":
                raise Conflict(f"review is already {review['status']}")
            entity_id: UUID | None = review["entity_id"]
            entity = None
            if entity_id is not None:
                entity = conn.execute(
                    "SELECT entity_id, status, label, catalog_id FROM entity WHERE entity_id = %s FOR UPDATE",
                    (entity_id,),
                ).fetchone()
            if entity is not None and entity["status"] == "merged" and body.action != "dismiss":
                raise Conflict("the review's entity has been merged; resolve against the surviving entity")

            resolution: dict[str, Any] = {"action": body.action, "by": actor, "note": body.note}
            if body.action in ("assign_catalog", "new_catalog"):
                if body.action == "new_catalog":
                    assert body.new_catalog is not None
                    created = insert_catalog_item(conn, body.new_catalog)
                    write_audit(
                        conn,
                        "create",
                        [entity_id] if entity_id else [],
                        actor,
                        {
                            "catalog_item": {k: v for k, v in created.items() if k != "created_at"},
                            "via_review": str(review_id),
                        },
                    )
                    catalog_id, display_name = created["catalog_id"], created["display_name"]
                else:
                    row = conn.execute(
                        "SELECT catalog_id, display_name FROM catalog_item WHERE catalog_id = %s", (body.catalog_id,)
                    ).fetchone()
                    if row is None:
                        raise Unprocessable(f"unknown catalog_id {body.catalog_id!r}")
                    catalog_id, display_name = row["catalog_id"], row["display_name"]
                resolution["catalog_id"] = catalog_id
                label = body.label or display_name
                resolution["label"] = label
                if entity is not None:
                    conn.execute(
                        """UPDATE entity SET catalog_id = %s, status = 'confirmed', label = %s, needs_review = FALSE,
                               review_reason = NULL, updated_at = now() WHERE entity_id = %s""",
                        (catalog_id, label, entity_id),
                    )
                    write_audit(
                        conn,
                        "classify",
                        [entity_id],
                        actor,
                        {
                            "review_id": str(review_id),
                            "before": {
                                "status": entity["status"],
                                "label": entity["label"],
                                "catalog_id": entity["catalog_id"],
                            },
                            "after": {"status": "confirmed", "label": label, "catalog_id": catalog_id},
                            "note": body.note,
                        },
                    )
                new_status = "resolved"
            elif body.action == "merge_into":
                assert body.target_entity_id is not None
                if entity is None:
                    raise Unprocessable("review has no entity to merge")
                try:
                    merge_entities(
                        conn,
                        entity_id,
                        body.target_entity_id,
                        actor,
                        note=body.note,
                        keep_review_id=review_id,
                        extra_detail={"review_id": str(review_id), "source_previous_needs_review": True},
                    )
                except NotFound as exc:
                    raise Unprocessable(exc.detail) from exc
                conn.execute(
                    "UPDATE entity SET needs_review = FALSE, review_reason = NULL WHERE entity_id = %s", (entity_id,)
                )
                resolution["target_entity_id"] = str(body.target_entity_id)
                new_status = "resolved"
            else:  # dismiss
                if entity is not None and entity["status"] != "merged":
                    conn.execute(
                        """UPDATE entity SET status = 'dismissed', needs_review = FALSE, review_reason = NULL,
                               updated_at = now() WHERE entity_id = %s""",
                        (entity_id,),
                    )
                write_audit(
                    conn,
                    "dismiss",
                    [entity_id] if entity_id else [],
                    actor,
                    {
                        "review_id": str(review_id),
                        "before": {"status": entity["status"]} if entity is not None else None,
                        "note": body.note,
                    },
                )
                new_status = "dismissed"

            updated = conn.execute(
                f"""UPDATE review_item SET status = %s, resolution = %s, resolved_by = %s, resolved_at = now()
                     WHERE review_id = %s RETURNING {REVIEW_COLUMNS}""",
                (new_status, jsonb(resolution), actor, review_id),
            ).fetchone()
        result = _expand(conn, [updated], ctx.bucket)[0]
    logger.info("review resolved", extra={"review_id": str(review_id), "action": body.action, "actor": actor})
    return result
