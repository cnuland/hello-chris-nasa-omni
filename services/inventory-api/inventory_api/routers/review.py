"""AC-API-12 review queue and AC-API-13 resolve actions."""

from __future__ import annotations

import logging
from typing import Any, Literal
from uuid import UUID

from fastapi import APIRouter, Query, Response

from ..catalog_ops import insert_catalog_item
from ..deps import Ctx, operator_actor
from ..errors import Conflict, NotFound, Unprocessable
from ..merging import merge_entities
from ..repo import (
    HIDDEN_STATUSES,
    fetch_items,
    jsonb,
    like_pattern,
    location_subtree_sql,
    no_capture_locations,
    normalised_bbox,
    real,
    write_audit,
)
from ..schemas import ResolveRequest, ReviewKind, ReviewStatus
from ..storage import uri_to_url
from ..structure_memory import is_not_inventory, record_not_inventory

logger = logging.getLogger(__name__)
router = APIRouter(tags=["review"])

REVIEW_COLUMNS = """review_id, kind, entity_id, candidate_ids, image_uri, description, observed_at, location_id,
                    model_guess, probabilities, status, resolution, resolved_by, resolved_at, created_at"""

MAX_SUGGESTED = 8

# Normalised label key (AC-API-25): lower case with spaces and hyphens removed.
_KEY_SQL = "regexp_replace(lower({col}), '[[:space:]-]+', '', 'g')"

# Module of every location (the nearest ancestor of kind 'module', self included).
_MODULE_OF_CTE = """
    anc(start_id, location_id, parent_id, kind, depth) AS (
        SELECT location_id, location_id, parent_id, kind, 0 FROM location
        UNION ALL
        SELECT a.start_id, l.location_id, l.parent_id, l.kind, a.depth + 1
          FROM anc a JOIN location l ON l.location_id = a.parent_id
         WHERE a.kind <> 'module' AND a.depth < 32
    ),
    module_of AS (SELECT start_id AS location_id, location_id AS module_id FROM anc WHERE kind = 'module')
"""

# The subject's best exemplar: highest quality, then newest (the same order as the exemplar cap).
_BEST_EXEMPLAR_SQL = """(SELECT bx.embedding FROM entity_exemplar bx WHERE bx.entity_id = s.entity_id
                          ORDER BY bx.quality DESC, bx.created_at DESC, bx.exemplar_id DESC LIMIT 1)"""

# AC-API-35 ranking weights. score = W_APPEARANCE * max(cosine, 0)        (only when both sides have exemplars)
#                                  + W_TEXT * pg_trgm similarity(label + description)
#                                  + W_ZONE (same zone) or W_MODULE (same module, other zone)
#                                  + W_TIME / (1 + |delta last_seen_at| in hours)
W_APPEARANCE, W_TEXT, W_ZONE, W_MODULE, W_TIME = 1.0, 0.5, 0.3, 0.15, 0.2
# match_reasons thresholds: cosine, description-only trigram similarity, |delta last_seen_at| in seconds.
REASON_APPEARANCE, REASON_DESCRIPTION, REASON_TIME_S = 0.7, 0.3, 3600.0

_SUBJ_COLUMNS = f"""s.entity_id, s.kind, s.catalog_id, {_KEY_SQL.format(col="s.label")} AS key, mo.module_id,
           s.last_location, sl.kind AS location_kind, s.last_seen_at,
           lower(s.label || ' ' || coalesce(s.description, '')) AS text,
           nullif(btrim(lower(s.description)), '') AS descr,
           {_BEST_EXEMPLAR_SQL} AS best"""
_SUBJ_FROM = """entity s LEFT JOIN module_of mo ON mo.location_id = s.last_location
                LEFT JOIN location sl ON sl.location_id = s.last_location"""

# Score components of candidate e for subject subj (used for suggestions and for stored candidates).
_COMPONENTS_SQL = """
        SELECT e.entity_id, e.last_seen_at, sim.similarity,
               similarity(subj.text, lower(e.label || ' ' || coalesce(e.description, ''))) AS text_sim,
               CASE WHEN subj.descr IS NOT NULL AND nullif(btrim(e.description), '') IS NOT NULL
                    THEN similarity(subj.descr, lower(e.description)) END AS desc_sim,
               (subj.last_location IS NOT NULL AND e.last_location = subj.last_location
                AND subj.location_kind IN ('zone', 'container')) AS same_zone,
               (subj.module_id IS NOT NULL AND em.module_id = subj.module_id) AS same_module,
               abs(extract(epoch FROM e.last_seen_at - subj.last_seen_at)) AS dt
          FROM entity e
          LEFT JOIN catalog_item ci ON ci.catalog_id = e.catalog_id
          LEFT JOIN module_of em ON em.location_id = e.last_location
          LEFT JOIN LATERAL (
                SELECT max(1 - (x.embedding <=> subj.best)) AS similarity
                  FROM entity_exemplar x WHERE x.entity_id = e.entity_id AND subj.best IS NOT NULL
          ) sim ON TRUE
         WHERE {where}
"""

_SCORE_SQL = f"""(
      {W_APPEARANCE} * COALESCE(GREATEST(f.similarity, 0), 0)
    + {W_TEXT} * f.text_sim
    + CASE WHEN f.same_zone THEN {W_ZONE} WHEN f.same_module THEN {W_MODULE} ELSE 0 END
    + COALESCE({W_TIME} / (1 + f.dt / 3600.0), 0)
)"""

_POOL_WHERE = f"""e.entity_id <> subj.entity_id
           AND e.status <> ALL(%(hidden)s)
           AND e.kind = subj.kind
           AND CASE WHEN subj.catalog_id IS NOT NULL THEN e.catalog_id = subj.catalog_id
                    ELSE subj.key <> '' AND ({_KEY_SQL.format(col="e.label")} = subj.key
                                             OR {_KEY_SQL.format(col="ci.class_name")} = subj.key) END"""

_RESULT_COLUMNS = "c.entity_id, c.similarity, c.score, c.desc_sim, c.same_zone, c.same_module, c.dt"

# AC-API-25/35: up to 8 suggested duplicates per subject, ranked by score, for many subjects in ONE statement.
_SUGGEST_SQL = f"""
WITH RECURSIVE {_MODULE_OF_CTE},
subj AS (SELECT {_SUBJ_COLUMNS} FROM {_SUBJ_FROM} WHERE s.entity_id = ANY(%(subjects)s::uuid[]))
SELECT subj.entity_id AS subject_id, {_RESULT_COLUMNS}
  FROM subj
  CROSS JOIN LATERAL (
        SELECT f.*, {_SCORE_SQL} AS score FROM ({_COMPONENTS_SQL.format(where=_POOL_WHERE)}) f
         ORDER BY score DESC, f.similarity DESC NULLS LAST, f.last_seen_at DESC NULLS LAST, f.entity_id
         LIMIT %(cap)s
  ) c
 ORDER BY subj.entity_id, c.score DESC, c.similarity DESC NULLS LAST, c.last_seen_at DESC NULLS LAST, c.entity_id
"""

# The same score for stored candidates (their stored order is kept), for all pairs in one statement.
_PAIR_SCORE_SQL = f"""
WITH RECURSIVE {_MODULE_OF_CTE},
pairs AS (
    SELECT DISTINCT p.subject_id, p.candidate_id FROM unnest(%(subjects)s::uuid[], %(candidates)s::uuid[])
        AS p(subject_id, candidate_id)
),
subj AS (SELECT {_SUBJ_COLUMNS} FROM {_SUBJ_FROM} WHERE s.entity_id IN (SELECT subject_id FROM pairs))
SELECT pairs.subject_id, {_RESULT_COLUMNS}
  FROM pairs JOIN subj ON subj.entity_id = pairs.subject_id
  CROSS JOIN LATERAL (
        SELECT f.*, {_SCORE_SQL} AS score FROM ({_COMPONENTS_SQL.format(where="e.entity_id = pairs.candidate_id")}) f
  ) c
"""

# AC-API-26/37: bbox (and its source) of the sighting of the review's entity whose frame is the review image, nearest
# observed_at.
_IMAGE_BBOX_SQL = """
SELECT r.review_id, sg.bbox, sg.bbox_source
  FROM unnest(%(reviews)s::uuid[], %(entities)s::uuid[], %(uris)s::text[], %(observed)s::timestamptz[])
       AS r(review_id, entity_id, image_uri, observed_at)
  CROSS JOIN LATERAL (
        SELECT s.bbox, s.bbox_source FROM sighting s
         WHERE s.entity_id = r.entity_id AND s.frame_uri = r.image_uri AND s.bbox IS NOT NULL
         ORDER BY abs(extract(epoch FROM s.seen_at - r.observed_at)), s.sighting_id DESC
         LIMIT 1
  ) sg
"""


def _match_reasons(row: dict[str, Any]) -> list[str]:
    """AC-API-35: why a candidate is suggested, in a fixed order."""
    reasons: list[str] = []
    if row["similarity"] is not None and row["similarity"] >= REASON_APPEARANCE:
        reasons.append("similar appearance")
    if row["desc_sim"] is not None and row["desc_sim"] >= REASON_DESCRIPTION:
        reasons.append("similar description")
    if row["same_zone"]:
        reasons.append("same zone")
    elif row["same_module"]:
        reasons.append("same module")
    if row["dt"] is not None and float(row["dt"]) <= REASON_TIME_S:
        reasons.append("seen around the same time")
    return reasons


def _ranking(row: dict[str, Any]) -> dict[str, Any]:
    return {"similarity": real(row["similarity"]), "score": real(row["score"]), "match_reasons": _match_reasons(row)}


_UNSCORED: dict[str, Any] = {"similarity": None, "score": None, "match_reasons": []}


def _review_entity(item: dict[str, Any], counts: dict[str, int], private: set[str]) -> dict[str, Any]:
    """Item + thumbnail_url + sighting_count (AC-API-24); no image from no_capture locations (AC-API-27)."""
    out = dict(item)
    if out.get("last_location") in private:
        out["snapshot_url"] = None
        out["snapshot_bbox"] = None
        out["snapshot_bbox_source"] = None
        out["snapshot_withheld_reason"] = "private_window"  # AC-API-44
    out["thumbnail_url"] = out["snapshot_url"]
    out["sighting_count"] = counts.get(str(out["entity_id"]), 0)
    return out


def _expand(conn, rows: list[dict[str, Any]], bucket: str) -> list[dict[str, Any]]:
    """Serialize review rows with subject, candidates (stored or suggested) and image_bbox, in a fixed number of
    queries whatever the number of reviews."""
    private = no_capture_locations(conn)
    stored: dict[str, list[str]] = {}
    to_suggest: list[str] = []
    for r in rows:
        rid = str(r["review_id"])
        stored[rid] = [str(c) for c in r["candidate_ids"] or []]
        if not stored[rid] and r["kind"] == "possible_duplicate" and r["entity_id"] is not None:
            to_suggest.append(str(r["entity_id"]))

    suggestions: dict[str, list[tuple[str, dict[str, Any]]]] = {}
    if to_suggest:
        for sg in conn.execute(
            _SUGGEST_SQL,
            {"subjects": list(dict.fromkeys(to_suggest)), "hidden": list(HIDDEN_STATUSES), "cap": MAX_SUGGESTED},
        ):
            suggestions.setdefault(str(sg["subject_id"]), []).append((str(sg["entity_id"]), _ranking(sg)))

    pair_subjects: list[str] = []
    pair_candidates: list[str] = []
    for r in rows:
        if r["entity_id"] is not None:
            for c in stored[str(r["review_id"])]:
                pair_subjects.append(str(r["entity_id"]))
                pair_candidates.append(c)
    pair_ranking: dict[tuple[str, str], dict[str, Any]] = {}
    if pair_subjects:
        for p in conn.execute(_PAIR_SCORE_SQL, {"subjects": pair_subjects, "candidates": pair_candidates}):
            pair_ranking[(str(p["subject_id"]), str(p["entity_id"]))] = _ranking(p)

    with_entity = [r for r in rows if r["entity_id"] is not None and r["image_uri"]]
    bboxes: dict[str, tuple[list[float], str | None]] = {}
    if with_entity:
        for b in conn.execute(
            _IMAGE_BBOX_SQL,
            {
                "reviews": [str(r["review_id"]) for r in with_entity],
                "entities": [str(r["entity_id"]) for r in with_entity],
                "uris": [r["image_uri"] for r in with_entity],
                "observed": [r["observed_at"] for r in with_entity],
            },
        ):
            box = normalised_bbox(b["bbox"])
            if box is not None:
                bboxes[str(b["review_id"])] = (box, b["bbox_source"])

    ids = {str(r["entity_id"]) for r in rows if r["entity_id"] is not None}
    ids |= {c for cs in stored.values() for c in cs}
    ids |= {c for cs in suggestions.values() for c, _ in cs}
    ordered_ids = sorted(ids)
    items = {str(i["entity_id"]): i for i in fetch_items(conn, ordered_ids, bucket, uri_to_url)}
    counts: dict[str, int] = {}
    if ordered_ids:
        counts = {
            str(c["entity_id"]): c["n"]
            for c in conn.execute(
                "SELECT entity_id, count(*) AS n FROM sighting WHERE entity_id = ANY(%s::uuid[]) GROUP BY entity_id",
                (ordered_ids,),
            )
        }

    out = []
    for r in rows:
        rid = str(r["review_id"])
        subject_id = str(r["entity_id"]) if r["entity_id"] is not None else None
        if stored[rid]:
            source = "stored"
            picked = [(c, pair_ranking.get((subject_id, c), _UNSCORED) if subject_id else _UNSCORED)
                      for c in stored[rid]]
        elif subject_id is not None and suggestions.get(subject_id) and r["kind"] == "possible_duplicate":
            source = "suggested"
            picked = suggestions[subject_id]
        else:
            source, picked = "none", []
        candidates = [{**_review_entity(items[c], counts, private), **rank} for c, rank in picked if c in items]
        subject = _review_entity(items[subject_id], counts, private) if subject_id and subject_id in items else None
        out.append(_serialize_review(r, candidates, source, subject, bboxes.get(rid), private, bucket))
    return out


def _serialize_review(
    row: dict[str, Any],
    candidates: list[dict[str, Any]],
    candidates_source: str,
    subject: dict[str, Any] | None,
    image_bbox: tuple[list[float], str | None] | None,
    private: set[str],
    bucket: str,
) -> dict[str, Any]:
    # Defence in depth (AC-API-27): ingest refuses reviews in no_capture zones, but never serve one if it exists.
    hidden_image = row["location_id"] in private
    return {
        "review_id": row["review_id"],
        "kind": row["kind"],
        "entity_id": row["entity_id"],
        "candidate_ids": [str(c) for c in row["candidate_ids"] or []],
        "candidates": candidates,
        "candidates_source": candidates_source,
        "subject": subject,
        # Only URLs /media can serve; arbitrary stored strings never reach the dashboard.
        "image_url": None if hidden_image else uri_to_url(row["image_uri"], bucket),
        "image_bbox": None if hidden_image or image_bbox is None else image_bbox[0],
        # AC-API-37: 'detector' (solid box), 'omni' (approximate box) or null (unknown / legacy sighting)
        "image_bbox_source": None if hidden_image or image_bbox is None else image_bbox[1],
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


_REVIEW_SORT = {
    "created_at": "r.created_at {o}, r.observed_at {o}",
    "observed_at": "r.observed_at {o}, r.created_at {o}",
}


@router.get("/review")
def list_reviews(
    ctx: Ctx,
    response: Response,
    status: ReviewStatus = "open",
    kind: ReviewKind | None = None,
    location: str | None = Query(None, max_length=64),
    q: str | None = Query(None, max_length=200),
    sort: Literal["created_at", "observed_at"] = "created_at",
    order: Literal["desc", "asc"] = "desc",
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
) -> list[dict[str, Any]]:
    where = ["r.status = %(status)s"]
    params: dict[str, Any] = {"status": status}
    if kind:
        where.append("r.kind = %(kind)s")
        params["kind"] = kind
    if location:
        where.append(f"r.location_id IN ({location_subtree_sql('location')})")
        params["location"] = location
    q_clean = q.strip() if q else ""
    if q_clean:
        where.append("(r.description ILIKE %(pat)s OR e.label ILIKE %(pat)s)")
        params["pat"] = like_pattern(q_clean)
    from_sql = "FROM review_item r LEFT JOIN entity e ON e.entity_id = r.entity_id WHERE " + " AND ".join(where)
    # sort/order are Literal-validated and only select from fixed SQL fragments (never interpolated from input).
    order_sql = _REVIEW_SORT[sort].format(o="ASC" if order == "asc" else "DESC") + ", r.review_id"
    with ctx.db.connection() as conn:
        total = conn.execute(f"SELECT count(*) AS n {from_sql}", params).fetchone()["n"]
        rows = conn.execute(
            f"SELECT {', '.join('r.' + c.strip() for c in REVIEW_COLUMNS.split(','))} {from_sql} "
            f"ORDER BY {order_sql} LIMIT %(limit)s OFFSET %(offset)s",
            dict(params, limit=limit, offset=offset),
        ).fetchall()
        result = _expand(conn, rows, ctx.bucket)
    response.headers["X-Total-Count"] = str(total)
    return result


@router.get("/review/{review_id}")
def get_review(review_id: UUID, ctx: Ctx) -> dict[str, Any]:
    with ctx.db.connection() as conn:
        row = conn.execute(f"SELECT {REVIEW_COLUMNS} FROM review_item WHERE review_id = %s", (review_id,)).fetchone()
        if row is None:
            raise NotFound(f"review {review_id} not found")
        return _expand(conn, [row], ctx.bucket)[0]


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
                    # AC-API-41: an exemplar cut from a sighting whose crop overlapped a person never joins a
                    # catalog class (it would match people as this item)
                    withheld = [
                        r["exemplar_id"]
                        for r in conn.execute(
                            """DELETE FROM entity_exemplar x USING sighting s
                                WHERE x.entity_id = %s AND x.sighting_id = s.sighting_id AND s.person_overlap
                               RETURNING x.exemplar_id""",
                            (entity_id,),
                        )
                    ]
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
                            "withheld_exemplar_ids": sorted(withheld),
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
                detail: dict[str, Any] = {
                    "review_id": str(review_id),
                    "before": {"status": entity["status"]} if entity is not None else None,
                    "note": body.note,
                }
                if entity is not None and entity["status"] != "merged" and is_not_inventory(body.note):
                    # AC-API-46 (UI6-05): remember what the operator called station structure, where and how it looked
                    memory_id = record_not_inventory(conn, review, entity_id, actor)
                    if memory_id is not None:
                        detail["structure_memory_id"] = memory_id
                write_audit(conn, "dismiss", [entity_id] if entity_id else [], actor, detail)
                new_status = "dismissed"

            updated = conn.execute(
                f"""UPDATE review_item SET status = %s, resolution = %s, resolved_by = %s, resolved_at = now()
                     WHERE review_id = %s RETURNING {REVIEW_COLUMNS}""",
                (new_status, jsonb(resolution), actor, review_id),
            ).fetchone()
        result = _expand(conn, [updated], ctx.bucket)[0]
    logger.info("review resolved", extra={"review_id": str(review_id), "action": body.action, "actor": actor})
    return result
