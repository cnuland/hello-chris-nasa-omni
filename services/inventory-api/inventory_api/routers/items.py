"""AC-API-2..11: items list/search, add (image required), patch, merge, detail, history."""

from __future__ import annotations

import logging
import uuid
from datetime import datetime
from typing import Annotated, Any, Literal
from uuid import UUID

import psycopg
from fastapi import APIRouter, File, Form, Query, UploadFile
from fastapi.concurrency import run_in_threadpool
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse

from ..deps import AppContext, Ctx, operator_actor
from ..errors import Conflict, NotFound, Unprocessable
from ..images import sniff_image
from ..merging import merge_entities, unmerge_entity
from ..repo import (
    EVENT_COLUMNS,
    EVENT_SELECT,
    FUZZY_LABEL_THRESHOLD,
    HIDDEN_STATUSES,
    catalog_exists,
    event_actor_kind,
    event_location_role,
    fetch_item,
    fetch_items,
    jsonb,
    like_pattern,
    location_exists,
    location_no_capture,
    location_subtree_sql,
    real,
    serialize_event,
    write_audit,
)
from ..schemas import EntityKind, EntityStatus, ItemPatch, ItemSort, MergeRequest, UnmergeRequest
from ..storage import uri_to_url

logger = logging.getLogger(__name__)
router = APIRouter(tags=["items"])

DETAIL_LIMIT = 20

_SORT_SQL: dict[str, str] = {
    "last_seen_at": "e.last_seen_at ASC NULLS LAST",
    "-last_seen_at": "e.last_seen_at DESC NULLS LAST",
    # AC-API-36: case-insensitive whatever the database collation (the cluster's CNPG DB is C), then the exact label;
    # entity_id is appended to every order as the final tiebreak, so pages are stable.
    "label": "lower(e.label) ASC, e.label ASC",
    "-label": "lower(e.label) DESC, e.label DESC",
    "confidence": "e.confidence ASC",
    "-confidence": "e.confidence DESC",
}

# Matches label, description, catalog display name or any catalog synonym (case-insensitive substring), or a label
# that is trigram-similar to q (fuzzy match for typos).
_Q_MATCH = """(
    e.label ILIKE %(pat)s OR e.description ILIKE %(pat)s OR c.display_name ILIKE %(pat)s
    OR EXISTS (SELECT 1 FROM unnest(c.synonyms) syn WHERE syn ILIKE %(pat)s)
    OR similarity(e.label, %(q)s) > %(fuzzy)s
)"""

# Relevance: exact label > label substring > catalog name/synonym > description; trigram similarity breaks ties.
_Q_SCORE = """(
    CASE WHEN lower(e.label) = lower(%(q)s) THEN 4 ELSE 0 END
  + CASE WHEN e.label ILIKE %(pat)s THEN 2 ELSE 0 END
  + CASE WHEN c.display_name ILIKE %(pat)s
           OR EXISTS (SELECT 1 FROM unnest(c.synonyms) syn WHERE syn ILIKE %(pat)s) THEN 1.5 ELSE 0 END
  + CASE WHEN e.description ILIKE %(pat)s THEN 0.5 ELSE 0 END
  + similarity(e.label, %(q)s)
)"""


# ------------------------------------------------------------------ GET /items
@router.get("/items")
def list_items(
    ctx: Ctx,
    q: str | None = Query(None, max_length=200),
    kind: EntityKind | None = None,
    status: Annotated[list[EntityStatus] | None, Query()] = None,
    catalog_id: str | None = None,
    location: str | None = None,
    min_confidence: float | None = Query(None, ge=0.0, le=1.0),
    seen_after: datetime | None = None,
    seen_before: datetime | None = None,
    needs_review: bool | None = None,
    sort: ItemSort | None = None,
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
) -> dict[str, Any]:
    where: list[str] = []
    params: dict[str, Any] = {}
    if status:
        where.append("e.status = ANY(%(statuses)s)")
        params["statuses"] = list(dict.fromkeys(status))
    else:
        where.append("e.status <> ALL(%(hidden)s)")
        params["hidden"] = list(HIDDEN_STATUSES)
    if kind:
        where.append("e.kind = %(kind)s")
        params["kind"] = kind
    if catalog_id:
        where.append("e.catalog_id = %(catalog_id)s")
        params["catalog_id"] = catalog_id
    if min_confidence is not None:
        # REAL column: compare in REAL so an inclusive bound like 0.7 matches a stored 0.7.
        where.append("e.confidence >= %(min_conf)s::real")
        params["min_conf"] = min_confidence
    if seen_after is not None:
        where.append("e.last_seen_at >= %(seen_after)s")
        params["seen_after"] = seen_after
    if seen_before is not None:
        where.append("e.last_seen_at <= %(seen_before)s")
        params["seen_before"] = seen_before
    if needs_review is not None:
        where.append("e.needs_review = %(needs_review)s")
        params["needs_review"] = needs_review
    if location:
        where.append(f"e.last_location IN ({location_subtree_sql('location')})")
        params["location"] = location
    q_clean = q.strip() if q else ""
    if q_clean:
        where.append(_Q_MATCH)
        params["q"] = q_clean
        params["pat"] = like_pattern(q_clean)
        params["fuzzy"] = FUZZY_LABEL_THRESHOLD

    from_sql = "FROM entity e LEFT JOIN catalog_item c ON c.catalog_id = e.catalog_id WHERE " + " AND ".join(where)
    if sort is None and q_clean:
        order = f"{_Q_SCORE} DESC, e.last_seen_at DESC NULLS LAST"
    else:
        order = _SORT_SQL[sort or "-last_seen_at"]
    order += ", e.entity_id"

    with ctx.db.connection() as conn:
        total = conn.execute(f"SELECT count(*) AS n {from_sql}", params).fetchone()["n"]
        page_params = dict(params, limit=limit, offset=offset)
        ids = [
            r["entity_id"]
            for r in conn.execute(
                f"SELECT e.entity_id {from_sql} ORDER BY {order} LIMIT %(limit)s OFFSET %(offset)s", page_params
            )
        ]
        items = fetch_items(conn, ids, ctx.bucket, uri_to_url)
    return {"items": items, "total": total}


# ------------------------------------------------------------------ POST /items
@router.post("/items", status_code=201)
async def add_item(
    ctx: Ctx,
    label: str = Form(..., min_length=1, max_length=500),
    location_id: str = Form(..., min_length=1),
    description: str | None = Form(None),
    catalog_id: str | None = Form(None),
    kind: Literal["object", "person"] = Form("object"),
    operator: str | None = Form(None),
    image: Annotated[UploadFile | None, File()] = None,
) -> JSONResponse:
    if image is None:
        raise Unprocessable("image is required")
    data = await image.read(ctx.settings.max_upload_bytes + 1)
    if not data:
        raise Unprocessable("image is empty")
    if len(data) > ctx.settings.max_upload_bytes:
        raise Unprocessable("image is too large")
    declared = (image.content_type or "").split(";")[0].strip().lower()
    if not declared.startswith("image/"):
        raise Unprocessable(f"image must have an image/* content-type, got {declared or 'none'}")
    label = label.strip()
    if not label:
        raise Unprocessable("label must not be blank")
    sniffed = sniff_image(data)
    if sniffed is None:
        raise Unprocessable("uploaded file is not a recognised image")
    content_type, ext = sniffed
    item = await run_in_threadpool(
        _create_operator_item,
        ctx,
        label,
        location_id,
        description,
        catalog_id or None,
        kind,
        operator_actor(operator),
        data,
        content_type,
        ext,
    )
    return JSONResponse(jsonable_encoder(item), status_code=201)


def _create_operator_item(
    ctx: AppContext,
    label: str,
    location_id: str,
    description: str | None,
    catalog_id: str | None,
    kind: str,
    actor: str,
    data: bytes,
    content_type: str,
    ext: str,
) -> dict[str, Any]:
    entity_id = uuid.uuid4()
    # Validate on one pooled connection and give it back before the (slower) upload.
    with ctx.db.connection() as conn:
        if not location_exists(conn, location_id):
            raise Unprocessable(f"unknown location_id {location_id!r}")
        if location_no_capture(conn, location_id):
            # AC-API-19: no frames/photos are stored for crew quarters or the WHC.
            raise Unprocessable(f"location {location_id!r} is no_capture; photos cannot be stored there")
        if catalog_id is not None and not catalog_exists(conn, catalog_id):
            raise Unprocessable(f"unknown catalog_id {catalog_id!r}")
    key = f"items/{entity_id}/snapshot.{ext}"
    snapshot_uri = ctx.store.put(key, data, content_type)
    try:
        with ctx.db.connection() as conn:
            with conn.transaction():
                conn.execute(
                    """INSERT INTO entity (entity_id, catalog_id, kind, status, label, description, last_location,
                           last_seen_at, first_seen_at, confidence, snapshot_uri, created_by, location_set_at,
                           location_set_by)
                       VALUES (%s, %s, %s, 'confirmed', %s, %s, %s, now(), now(), 1.0, %s, 'operator', now(), %s)""",
                    (entity_id, catalog_id, kind, label, description, location_id, snapshot_uri, actor),
                )
                conn.execute(
                    """INSERT INTO event (event_type, entity_id, actor_desc, to_location, occurred_at, confidence,
                           evidence_uri, source) VALUES ('added', %s, %s, %s, now(), 1.0, %s, 'operator')""",
                    (entity_id, actor, location_id, snapshot_uri),
                )
                write_audit(
                    conn,
                    "create",
                    [entity_id],
                    actor,
                    {
                        "source": "operator",
                        "label": label,
                        "kind": kind,
                        "catalog_id": catalog_id,
                        "last_location": location_id,
                        "snapshot_uri": snapshot_uri,
                    },
                )
            item = fetch_item(conn, entity_id, ctx.bucket, uri_to_url)
    except psycopg.IntegrityError as exc:
        ctx.store.delete(key)
        raise Unprocessable(f"invalid item: {exc.diag.message_primary}") from exc
    except Exception:
        ctx.store.delete(key)
        raise
    logger.info("operator added item", extra={"entity_id": str(entity_id), "actor": actor})
    return item


# ------------------------------------------------------------------ GET /items/{id}
@router.get("/items/{entity_id}")
def get_item(entity_id: UUID, ctx: Ctx) -> dict[str, Any]:
    with ctx.db.connection() as conn:
        item = fetch_item(conn, entity_id, ctx.bucket, uri_to_url)
        sightings = conn.execute(
            """SELECT s.sighting_id, s.window_id, s.location_id, s.seen_at, s.bbox, s.bbox_source, s.confidence,
                      s.frame_uri, s.description, s.person_overlap, COALESCE(w.is_private, FALSE) AS is_private
                 FROM sighting s LEFT JOIN observation_window w ON w.window_id = s.window_id
                WHERE s.entity_id = %s ORDER BY s.seen_at DESC, s.sighting_id DESC LIMIT %s""",
            (entity_id, DETAIL_LIMIT),
        ).fetchall()
        events = conn.execute(
            EVENT_SELECT + " WHERE ev.entity_id = %s ORDER BY ev.occurred_at DESC, ev.created_at DESC LIMIT %s",
            (entity_id, DETAIL_LIMIT),
        ).fetchall()
        exemplar_count = conn.execute(
            "SELECT count(*) AS n FROM entity_exemplar WHERE entity_id = %s", (entity_id,)
        ).fetchone()["n"]
    item["sightings"] = [
        {
            "sighting_id": s["sighting_id"],
            "window_id": s["window_id"],
            "location_id": s["location_id"],
            "seen_at": s["seen_at"],
            "bbox": [real(b) for b in s["bbox"]] if s["bbox"] is not None else None,
            "bbox_source": s["bbox_source"] if s["bbox"] is not None else None,
            "confidence": real(s["confidence"]),
            "frame_url": uri_to_url(s["frame_uri"], ctx.bucket),
            "description": s["description"],
            "person_overlap": s["person_overlap"],  # AC-API-41: its crop overlapped a person and was not stored
            "is_private": s["is_private"],  # AC-API-44: seen in a private window (no image by design)
        }
        for s in sightings
    ]
    item["events"] = [serialize_event(e, ctx.bucket, uri_to_url) for e in events]
    item["exemplar_count"] = exemplar_count
    return item


# ------------------------------------------------------------------ PATCH /items/{id}
_PATCHABLE = ("label", "description", "catalog_id", "last_location", "status", "attributes", "needs_review")


@router.patch("/items/{entity_id}")
def patch_item(entity_id: UUID, body: ItemPatch, ctx: Ctx) -> dict[str, Any]:
    actor = operator_actor(body.operator)
    fields = [f for f in _PATCHABLE if f in body.model_fields_set]
    with ctx.db.connection() as conn:
        with conn.transaction():
            current = conn.execute(
                f"SELECT {', '.join(_PATCHABLE)} FROM entity WHERE entity_id = %s FOR UPDATE",
                (entity_id,),
            ).fetchone()
            if current is None:
                raise NotFound(f"entity {entity_id} not found")
            if current["status"] == "merged":
                raise Conflict("entity is merged; edit the surviving entity instead")
            new_values = {f: getattr(body, f) for f in fields}
            if "last_location" in new_values and not location_exists(conn, new_values["last_location"]):
                raise Unprocessable(f"unknown location {new_values['last_location']!r}")
            if new_values.get("catalog_id") is not None and not catalog_exists(conn, new_values["catalog_id"]):
                raise Unprocessable(f"unknown catalog_id {new_values['catalog_id']!r}")

            if new_values:
                assignments = ", ".join(f"{f} = %({f})s" for f in new_values)
                params = {f: (jsonb(v) if f == "attributes" else v) for f, v in new_values.items()}
                params["entity_id"] = entity_id
                conn.execute(
                    f"UPDATE entity SET {assignments}, updated_at = now() WHERE entity_id = %(entity_id)s", params
                )
            old_location = current["last_location"]
            new_location = new_values.get("last_location")
            if new_location is not None and new_location != old_location:
                # AC-API-31: the operator's correction is the newest knowledge about the location. Record that in
                # location_set_at (a late pipeline window with an older seen_at cannot move the item back) and leave
                # last_seen_at alone: it means "last camera sighting", which an edit is not.
                conn.execute(
                    "UPDATE entity SET location_set_at = now(), location_set_by = %s WHERE entity_id = %s",
                    (actor, entity_id),
                )
                conn.execute(
                    """INSERT INTO event (event_type, entity_id, actor_desc, from_location, to_location, occurred_at,
                           confidence, source) VALUES ('moved', %s, %s, %s, %s, now(), 1.0, 'operator')""",
                    (entity_id, actor, old_location, new_location),
                )
            write_audit(
                conn,
                "update",
                [entity_id],
                actor,
                {
                    "before": {f: current[f] for f in fields},
                    "after": new_values,
                    "note": body.note,
                },
            )
        item = fetch_item(conn, entity_id, ctx.bucket, uri_to_url)
    logger.info("item updated", extra={"entity_id": str(entity_id), "actor": actor, "fields": fields})
    return item


# ------------------------------------------------------------------ POST /items/{id}/merge
@router.post("/items/{entity_id}/merge")
def merge_item(entity_id: UUID, body: MergeRequest, ctx: Ctx) -> dict[str, Any]:
    actor = operator_actor(body.operator)
    with ctx.db.connection() as conn:
        with conn.transaction():
            detail = merge_entities(conn, entity_id, body.into_entity_id, actor, note=body.note)
        target = fetch_item(conn, body.into_entity_id, ctx.bucket, uri_to_url)
    logger.info("entities merged", extra={"source": str(entity_id), "target": str(body.into_entity_id), "actor": actor})
    return {"merged_entity_id": str(entity_id), "target": target, "moved": detail["moved"]}


# ------------------------------------------------------------------ POST /items/{id}/unmerge
@router.post("/items/{entity_id}/unmerge")
def unmerge_item(entity_id: UUID, body: UnmergeRequest, ctx: Ctx) -> dict[str, Any]:
    """AC-API-33: undo the merge of ``entity_id`` (the merged source) using the merge's audit detail."""
    actor = operator_actor(body.operator)
    with ctx.db.connection() as conn:
        with conn.transaction():
            result = unmerge_entity(conn, entity_id, actor, note=body.note)
        target_id = result["target_entity_id"]
        source = fetch_item(conn, entity_id, ctx.bucket, uri_to_url)
        target = fetch_item(conn, target_id, ctx.bucket, uri_to_url)
    logger.info("entities unmerged", extra={"source": str(entity_id), "target": target_id, "actor": actor})
    return {
        "unmerged_entity_id": str(entity_id),
        "target_entity_id": target_id,
        "source": source,
        "target": target,
        "restored": result["restored"],
    }


# ------------------------------------------------------------------ GET /items/{id}/history
def _fmt_value(value: Any) -> str:
    text = "null" if value is None else str(value)
    return text if len(text) <= 60 else text[:57] + "..."


def _audit_summary(a: dict[str, Any]) -> str:
    detail = a["detail"] or {}
    summary = f"{a['op']} by {a['actor']}"
    before, after = detail.get("before"), detail.get("after")
    if isinstance(before, dict) and isinstance(after, dict):
        changes = [
            f"{field}: {_fmt_value(before.get(field))} -> {_fmt_value(after[field])}"
            for field in after
            if before.get(field) != after[field]
        ]
        if changes:
            summary += " (" + "; ".join(changes) + ")"
    note = detail.get("note")
    return summary + (f": {note}" if note else "")


def _is_routine(a: dict[str, Any], entity_id: str, own_events: set[str], own_reviews: set[str]) -> bool:
    """A pipeline ingest audit that, for this entity, only refreshed the sighting fields (last_seen_at/confidence/
    snapshot): it did not create the entity and carried none of the entity's events or reviews."""
    if a["op"] != "update" or a["actor"] != "pipeline":
        return False
    detail = a["detail"] or {}
    if entity_id in {str(x) for x in detail.get("created_entity_ids") or []}:
        return False
    if own_events & {str(x) for x in detail.get("event_ids") or []}:
        return False
    return not own_reviews & {str(x) for x in detail.get("review_ids") or []}


def _collapse(timeline: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """AC-API-32: fold each run of >=2 consecutive routine pipeline audits into one entry with a count."""
    out: list[dict[str, Any]] = []
    run: list[dict[str, Any]] = []

    def flush() -> None:
        if len(run) == 1:
            out.append(run[0])
        elif run:
            newest, oldest = run[0], run[-1]
            out.append(
                {
                    **newest,
                    "summary": f"{len(run)} pipeline updates (sighting refresh: last_seen_at/confidence)",
                    "count": len(run),
                    "first_at": oldest["at"],
                    "audit_ids": [r["id"] for r in run],
                }
            )
        run.clear()

    for entry in timeline:
        if entry.pop("_routine", False):
            run.append(entry)
        else:
            flush()
            out.append(entry)
    flush()
    return out


@router.get("/items/{entity_id}/history")
def item_history(
    entity_id: UUID,
    ctx: Ctx,
    limit: int = Query(500, ge=1, le=2000),
    collapse: bool = True,
) -> list[dict[str, Any]]:
    bucket = ctx.bucket
    with ctx.db.connection() as conn:
        if conn.execute("SELECT 1 FROM entity WHERE entity_id = %s", (entity_id,)).fetchone() is None:
            raise NotFound(f"entity {entity_id} not found")
        sightings = conn.execute(
            "SELECT sighting_id, seen_at, location_id, confidence, frame_uri, description FROM sighting "
            "WHERE entity_id = %s ORDER BY seen_at DESC LIMIT %s",
            (entity_id, limit),
        ).fetchall()
        events = conn.execute(
            f"SELECT {EVENT_COLUMNS} FROM event WHERE entity_id = %s ORDER BY occurred_at DESC LIMIT %s",
            (entity_id, limit),
        ).fetchall()
        audits = conn.execute(
            "SELECT audit_id, op, actor, detail, at FROM audit_log WHERE %s = ANY(entity_ids) ORDER BY at DESC "
            "LIMIT %s",
            (entity_id, limit),
        ).fetchall()
        own_events: set[str] = set()
        own_reviews: set[str] = set()
        if collapse and any(a["actor"] == "pipeline" for a in audits):
            own_events = {
                str(r["event_id"])
                for r in conn.execute("SELECT event_id FROM event WHERE entity_id = %s", (entity_id,))
            }
            own_reviews = {
                str(r["review_id"])
                for r in conn.execute("SELECT review_id FROM review_item WHERE entity_id = %s", (entity_id,))
            }
    timeline: list[dict[str, Any]] = []
    for s in sightings:
        summary = f"Seen at {s['location_id'] or 'unknown location'} (confidence {real(s['confidence']):.2f})"
        if s["description"]:
            summary += f": {s['description']}"
        timeline.append(
            {
                "at": s["seen_at"],
                "kind": "sighting",
                "id": str(s["sighting_id"]),
                "location_id": s["location_id"],
                "summary": summary,
                "image_url": uri_to_url(s["frame_uri"], bucket),
            }
        )
    for e in events:
        # AC-API-38: a use/sighting/addition happened "at" one place; only moves read "from -> to".
        role = event_location_role(e["event_type"], e["from_location"], e["to_location"])
        summary = e["event_type"]
        if role == "from_to":
            summary += f" {e['from_location'] or '?'} -> {e['to_location'] or '?'}"
        elif role == "at":
            summary += f" at {e['to_location'] or e['from_location']}"
        if e["actor_desc"]:
            summary += f" by {e['actor_desc']}"
        timeline.append(
            {
                "at": e["occurred_at"],
                "kind": "event",
                "id": str(e["event_id"]),
                "location_id": e["to_location"] or e["from_location"],
                "summary": summary,
                "image_url": uri_to_url(e["evidence_uri"], bucket),
                "event_type": e["event_type"],
                "source": e["source"],
                "location_role": role,
                "actor_kind": event_actor_kind(e["source"], e["actor_desc"], e["actor_entity"]),
            }
        )
    key = str(entity_id)
    for a in audits:
        timeline.append(
            {
                "at": a["at"],
                "kind": "audit",
                "id": str(a["audit_id"]),
                "location_id": None,
                "summary": _audit_summary(a),
                "image_url": None,
                "op": a["op"],
                "actor": a["actor"],
                "count": 1,
                "_routine": collapse and _is_routine(a, key, own_events, own_reviews),
            }
        )
    timeline.sort(key=lambda t: t["at"], reverse=True)
    timeline = _collapse(timeline)
    return timeline[:limit]
