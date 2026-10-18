"""Entity merge (AC-API-9), shared by POST /items/{id}/merge and review merge_into (AC-API-13)."""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

import psycopg

from .errors import Conflict, NotFound
from .repo import write_audit

EXEMPLAR_CAP = 20  # per entity (AC-API-18); a merge must keep the invariant too


def merge_entities(
    conn: psycopg.Connection,
    source_id: UUID,
    target_id: UUID,
    actor: str,
    note: str | None = None,
    extra_detail: dict[str, Any] | None = None,
    keep_review_id: UUID | None = None,
) -> dict[str, Any]:
    """Merge ``source_id`` INTO ``target_id`` inside the caller's transaction.

    Raises Conflict for self-merge, an already merged source or target, a dismissed target, or different kinds;
    NotFound for unknown ids. Besides sightings/events/exemplars, the merge re-points every other reference to the
    source (event.actor_entity, entity.parent_entity, open reviews and review candidate lists), except the review
    ``keep_review_id`` that is resolving this merge. The exemplar cap is re-applied on the target. The audit row records
    everything needed to undo the merge, including the full rows of any exemplars dropped by the cap.
    """
    if source_id == target_id:
        raise Conflict("cannot merge an entity into itself")
    rows = conn.execute(
        "SELECT entity_id, kind, status, merged_into, last_seen_at, last_location, first_seen_at, needs_review, "
        "review_reason, location_set_at, location_set_by FROM entity WHERE entity_id = ANY(%s::uuid[]) ORDER BY entity_id FOR UPDATE",
        ([str(source_id), str(target_id)],),
    ).fetchall()
    by_id = {r["entity_id"]: r for r in rows}
    source, target = by_id.get(source_id), by_id.get(target_id)
    if source is None:
        raise NotFound(f"entity {source_id} not found")
    if target is None:
        raise NotFound(f"entity {target_id} not found")
    if source["status"] == "merged":
        raise Conflict("source entity is already merged")
    if target["status"] == "merged":
        raise Conflict("target entity is merged; merge into its survivor instead")
    if target["status"] == "dismissed":
        raise Conflict("target entity is dismissed; restore it before merging into it")
    if source["kind"] != target["kind"]:
        raise Conflict(f"cannot merge a {source['kind']} into a {target['kind']}")

    params = (str(target_id), str(source_id))
    # Undo (AC-API-33) treats target sightings with a higher id as recorded after this merge. Ingest locks the target
    # FOR UPDATE before inserting, so no sighting of these entities can get a lower id after this point.
    sighting_watermark = conn.execute("SELECT COALESCE(max(sighting_id), 0) AS m FROM sighting").fetchone()["m"]
    sighting_ids = [
        r["sighting_id"]
        for r in conn.execute("UPDATE sighting SET entity_id = %s WHERE entity_id = %s RETURNING sighting_id", params)
    ]
    event_ids = [
        str(r["event_id"])
        for r in conn.execute("UPDATE event SET entity_id = %s WHERE entity_id = %s RETURNING event_id", params)
    ]
    exemplar_ids = [
        r["exemplar_id"]
        for r in conn.execute(
            "UPDATE entity_exemplar SET entity_id = %s WHERE entity_id = %s RETURNING exemplar_id", params
        )
    ]

    # Re-apply the per-entity exemplar cap (keep the best 20; ties drop the oldest), keeping full rows for undo.
    moved_exemplars = set(exemplar_ids)
    dropped_exemplars = [
        {
            "exemplar_id": r["exemplar_id"],
            "entity_id": str(source_id) if r["exemplar_id"] in moved_exemplars else str(target_id),
            "embedding": r["embedding"],
            "quality": r["quality"],
            "crop_uri": r["crop_uri"],
            "created_at": r["created_at"].isoformat(),
            "sighting_id": r["sighting_id"],  # AC-API-41 link, restored on unmerge
        }
        for r in conn.execute(
            """DELETE FROM entity_exemplar WHERE exemplar_id IN (
                   SELECT exemplar_id FROM entity_exemplar WHERE entity_id = %s
                    ORDER BY quality DESC, created_at DESC, exemplar_id DESC OFFSET %s)
               RETURNING exemplar_id, embedding::text AS embedding, quality, crop_uri, created_at, sighting_id""",
            (str(target_id), EXEMPLAR_CAP),
        )
    ]

    actor_event_ids = [
        str(r["event_id"])
        for r in conn.execute("UPDATE event SET actor_entity = %s WHERE actor_entity = %s RETURNING event_id", params)
    ]
    child_entity_ids = [
        str(r["entity_id"])
        for r in conn.execute(
            "UPDATE entity SET parent_entity = %s, updated_at = now() WHERE parent_entity = %s AND entity_id <> %s "
            "RETURNING entity_id",
            (*params, str(target_id)),
        )
    ]
    keep = str(keep_review_id) if keep_review_id else None
    repointed_reviews = conn.execute(
        """UPDATE review_item SET entity_id = %s
            WHERE entity_id = %s AND status = 'open' AND review_id IS DISTINCT FROM %s::uuid
        RETURNING review_id, kind""",
        (*params, keep),
    ).fetchall()
    review_ids = [str(r["review_id"]) for r in repointed_reviews]
    candidate_reviews_previous = {
        str(r["review_id"]): [str(c) for c in r["candidate_ids"]]
        for r in conn.execute(
            """SELECT review_id, candidate_ids FROM review_item
                WHERE status = 'open' AND %s::uuid = ANY(candidate_ids) AND review_id IS DISTINCT FROM %s::uuid
                FOR UPDATE""",
            (str(source_id), keep),
        )
    }
    if candidate_reviews_previous:
        # Replace the source by the target, de-duplicating while keeping the original order.
        conn.execute(
            """UPDATE review_item SET candidate_ids = ARRAY(
                   SELECT c FROM unnest(array_replace(candidate_ids, %s::uuid, %s::uuid)) WITH ORDINALITY AS u(c, n)
                    GROUP BY c ORDER BY min(n))
                WHERE review_id = ANY(%s::uuid[])""",
            (str(source_id), str(target_id), list(candidate_reviews_previous)),
        )
    if repointed_reviews:
        conn.execute(
            "UPDATE entity SET needs_review = TRUE, review_reason = COALESCE(review_reason, %s), updated_at = now() "
            "WHERE entity_id = %s",
            (repointed_reviews[0]["kind"], str(target_id)),
        )

    source_newer = source["last_seen_at"] is not None and (
        target["last_seen_at"] is None or source["last_seen_at"] > target["last_seen_at"]
    )
    if source_newer:
        conn.execute(
            "UPDATE entity SET last_seen_at = %s, updated_at = now() WHERE entity_id = %s",
            (source["last_seen_at"], str(target_id)),
        )
    # AC-API-31: the location follows whichever entity's location was set most recently (an operator move on the
    # target is not undone by an older sighting of the source). Legacy rows without provenance use last_seen_at.
    source_loc_at = source["location_set_at"] or source["last_seen_at"]
    target_loc_at = target["location_set_at"] or target["last_seen_at"]
    if (
        source["last_location"] is not None
        and source_loc_at is not None
        and (target_loc_at is None or source_loc_at > target_loc_at)
    ):
        conn.execute(
            "UPDATE entity SET last_location = %s, location_set_at = %s, location_set_by = %s, updated_at = now() "
            "WHERE entity_id = %s",
            (source["last_location"], source_loc_at, source["location_set_by"] or "pipeline", str(target_id)),
        )
    conn.execute(
        "UPDATE entity SET first_seen_at = LEAST(first_seen_at, %s), updated_at = now() WHERE entity_id = %s",
        (source["first_seen_at"], str(target_id)),
    )
    conn.execute(
        "UPDATE entity SET status = 'merged', merged_into = %s, updated_at = now() WHERE entity_id = %s", params
    )

    detail: dict[str, Any] = {
        "source_entity_id": str(source_id),
        "target_entity_id": str(target_id),
        "source_previous": {
            "status": source["status"],
            "merged_into": None,
            "needs_review": source["needs_review"],
            "review_reason": source["review_reason"],
        },
        "target_previous": {
            "last_seen_at": target["last_seen_at"].isoformat() if target["last_seen_at"] else None,
            "last_location": target["last_location"],
            "location_set_at": target["location_set_at"].isoformat() if target["location_set_at"] else None,
            "location_set_by": target["location_set_by"],
            "first_seen_at": target["first_seen_at"].isoformat() if target["first_seen_at"] else None,
            "needs_review": target["needs_review"],
            "review_reason": target["review_reason"],
        },
        "moved": {
            "sighting_ids": sighting_ids,
            "event_ids": event_ids,
            "exemplar_ids": exemplar_ids,
            "actor_event_ids": actor_event_ids,
            "child_entity_ids": child_entity_ids,
            "review_ids": review_ids,
            "candidate_review_ids": list(candidate_reviews_previous),
            "candidate_reviews_previous": candidate_reviews_previous,
        },
        "dropped_exemplars": dropped_exemplars,
        "sighting_watermark": sighting_watermark,
        "note": note,
    }
    if extra_detail:
        detail.update(extra_detail)
    write_audit(conn, "merge", [source_id, target_id], actor, detail)
    return detail


def _ts(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value else None


def _after_merge_candidates(previous: list[str], source: str, target: str) -> list[str]:
    """The candidate list a merge produced from ``previous`` (source replaced by target, de-duplicated in order)."""
    out: list[str] = []
    for c in previous:
        c = target if c == source else c
        if c not in out:
            out.append(c)
    return out


def _sightings_from_source(
    conn: psycopg.Connection,
    source_id: str,
    target_id: str,
    moved_exemplars: list[int],
    merged_at: datetime,
    watermark: int | None,
) -> list[int]:
    """Sightings recorded on the target after the merge whose appearance matches the source better than the target.

    "After the merge" is a sighting id above the merge's ``sighting_watermark`` (older merge rows without one: a window
    created after the merge). A sighting's appearance is the exemplar its ingest stored on the target in the same
    transaction (same created_at as its window). It "came from the source" when its best cosine similarity to the
    source's moved exemplars is higher than to the target's own exemplars. Sightings without an exemplar (omni-only
    boxes) cannot be attributed; they stay on the target.
    """
    rows = conn.execute(
        """WITH post AS (
               SELECT s.sighting_id, w.created_at AS ingested_at FROM sighting s
                 JOIN observation_window w ON w.window_id = s.window_id
                WHERE s.entity_id = %(t)s
                  AND CASE WHEN %(wm)s::bigint IS NOT NULL THEN s.sighting_id > %(wm)s::bigint
                           ELSE w.created_at > %(at)s END
           ), probe AS (
               SELECT post.sighting_id, x.exemplar_id, x.embedding FROM post
                 JOIN entity_exemplar x ON x.entity_id = %(t)s AND x.created_at = post.ingested_at
                WHERE NOT x.exemplar_id = ANY(%(moved)s::bigint[])
           ), src AS (
               SELECT embedding FROM entity_exemplar WHERE entity_id = %(t)s AND exemplar_id = ANY(%(moved)s::bigint[])
           ), own AS (
               SELECT embedding FROM entity_exemplar
                WHERE entity_id = %(t)s AND NOT exemplar_id = ANY(%(moved)s::bigint[])
                  AND exemplar_id NOT IN (SELECT exemplar_id FROM probe)
           )
           SELECT probe.sighting_id,
                  (SELECT max(1 - (probe.embedding <=> src.embedding)) FROM src) AS to_source,
                  (SELECT max(1 - (probe.embedding <=> own.embedding)) FROM own) AS to_target
             FROM probe""",
        {"t": target_id, "at": merged_at, "moved": moved_exemplars, "wm": watermark},
    ).fetchall()
    return sorted(
        {
            r["sighting_id"]
            for r in rows
            if r["to_source"] is not None and (r["to_target"] is None or r["to_source"] > r["to_target"])
        }
    )


def unmerge_entity(
    conn: psycopg.Connection, source_id: UUID, actor: str, note: str | None = None
) -> dict[str, Any]:
    """Undo the merge of ``source_id`` into its survivor inside the caller's transaction (AC-API-33).

    Uses the latest merge audit row for (source, target). Only rows that are still where the merge put them are moved
    back, and target fields are restored only when they still hold the value the merge gave them (a later sighting or
    operator edit of the target is kept). Raises NotFound for an unknown id and Conflict when the entity is not merged,
    the target has been merged since, the merge has no usable audit detail, or new target sightings came from the
    source's exemplars. Writes a 'split' audit row.
    """
    head = conn.execute("SELECT entity_id, status, merged_into FROM entity WHERE entity_id = %s", (str(source_id),))
    row = head.fetchone()
    if row is None:
        raise NotFound(f"entity {source_id} not found")
    if row["status"] != "merged" or row["merged_into"] is None:
        raise Conflict(f"entity is {row['status']}, not merged; nothing to unmerge")
    target_id = row["merged_into"]
    locked = {
        r["entity_id"]: r
        for r in conn.execute(
            "SELECT entity_id, status, merged_into, label, last_seen_at, last_location, location_set_at, "
            "location_set_by, first_seen_at, needs_review, review_reason FROM entity "
            "WHERE entity_id = ANY(%s::uuid[]) ORDER BY entity_id FOR UPDATE",
            ([str(source_id), str(target_id)],),
        )
    }
    source, target = locked[source_id], locked[target_id]
    if source["status"] != "merged" or source["merged_into"] != target_id:
        raise Conflict("entity changed while unmerging; retry")
    if target["status"] == "merged":
        raise Conflict(
            f"target {target_id} has itself been merged into {target['merged_into']} since; unmerge that first"
        )
    s, t = str(source_id), str(target_id)
    audit = conn.execute(
        """SELECT audit_id, detail, at FROM audit_log
            WHERE op = 'merge' AND entity_ids @> ARRAY[%s::uuid, %s::uuid]
              AND detail->>'source_entity_id' = %s AND detail->>'target_entity_id' = %s
            ORDER BY audit_id DESC LIMIT 1""",
        (s, t, s, t),
    ).fetchone()
    detail = audit["detail"] if audit else None
    if not detail or not isinstance(detail.get("moved"), dict) or not isinstance(detail.get("target_previous"), dict):
        raise Conflict("no merge audit with undo detail exists for this entity; it cannot be unmerged automatically")
    moved = detail["moved"]
    sp, tp = detail.get("source_previous") or {}, detail["target_previous"]
    exemplar_ids = [int(x) for x in moved.get("exemplar_ids") or []]

    from_source = _sightings_from_source(conn, s, t, exemplar_ids, audit["at"], detail.get("sighting_watermark"))
    if from_source:
        raise Conflict(
            f"{len(from_source)} sighting(s) recorded on the target after the merge match the source's appearance "
            f"(sighting_ids {from_source}); unmerging would leave them on the wrong item"
        )

    def back(sql: str, ids: list[Any]) -> list[Any]:
        if not ids:
            return []
        return [r["id"] for r in conn.execute(sql, (s, ids, t)).fetchall()]

    restored: dict[str, Any] = {
        "sighting_ids": back(
            "UPDATE sighting SET entity_id = %s WHERE sighting_id = ANY(%s) AND entity_id = %s RETURNING sighting_id AS id",
            [int(x) for x in moved.get("sighting_ids") or []],
        ),
        "event_ids": [str(x) for x in back(
            "UPDATE event SET entity_id = %s WHERE event_id = ANY(%s::uuid[]) AND entity_id = %s RETURNING event_id AS id",
            moved.get("event_ids") or [],
        )],
        "exemplar_ids": back(
            "UPDATE entity_exemplar SET entity_id = %s WHERE exemplar_id = ANY(%s) AND entity_id = %s "
            "RETURNING exemplar_id AS id",
            exemplar_ids,
        ),
        "actor_event_ids": [str(x) for x in back(
            "UPDATE event SET actor_entity = %s WHERE event_id = ANY(%s::uuid[]) AND actor_entity = %s "
            "RETURNING event_id AS id",
            moved.get("actor_event_ids") or [],
        )],
        "child_entity_ids": [str(x) for x in back(
            "UPDATE entity SET parent_entity = %s, updated_at = now() WHERE entity_id = ANY(%s::uuid[]) "
            "AND parent_entity = %s RETURNING entity_id AS id",
            moved.get("child_entity_ids") or [],
        )],
        "review_ids": [str(x) for x in back(
            "UPDATE review_item SET entity_id = %s WHERE review_id = ANY(%s::uuid[]) AND entity_id = %s "
            "RETURNING review_id AS id",
            moved.get("review_ids") or [],
        )],
    }

    # Candidate lists: restored only when they are still exactly what the merge made of them.
    candidate_review_ids: list[str] = []
    previous_lists: dict[str, list[str]] = moved.get("candidate_reviews_previous") or {}
    if previous_lists:
        current = {
            str(r["review_id"]): [str(c) for c in r["candidate_ids"]]
            for r in conn.execute(
                "SELECT review_id, candidate_ids FROM review_item WHERE review_id = ANY(%s::uuid[]) FOR UPDATE",
                (list(previous_lists),),
            )
        }
        for rid, previous in previous_lists.items():
            if current.get(rid) == _after_merge_candidates(previous, s, t):
                conn.execute("UPDATE review_item SET candidate_ids = %s::uuid[] WHERE review_id = %s", (previous, rid))
                candidate_review_ids.append(rid)
    restored["candidate_review_ids"] = candidate_review_ids

    # Exemplars the merge dropped for the cap come back, then the cap is re-applied to both entities.
    reinserted: list[int] = []
    for d in detail.get("dropped_exemplars") or []:
        r = conn.execute(
            """INSERT INTO entity_exemplar (exemplar_id, entity_id, embedding, quality, crop_uri, created_at,
                   sighting_id)
               SELECT %s, %s, %s::halfvec, %s, %s, %s, (SELECT sighting_id FROM sighting WHERE sighting_id = %s)
                WHERE EXISTS (SELECT 1 FROM entity WHERE entity_id = %s)
               ON CONFLICT (exemplar_id) DO NOTHING RETURNING exemplar_id""",
            (d["exemplar_id"], d["entity_id"], d["embedding"], d["quality"], d["crop_uri"], d["created_at"],
             d.get("sighting_id"), d["entity_id"]),
        ).fetchone()
        if r is not None:
            reinserted.append(r["exemplar_id"])
    restored["reinserted_exemplar_ids"] = reinserted
    dropped_now = [
        {
            "exemplar_id": r["exemplar_id"],
            "entity_id": str(r["entity_id"]),
            "embedding": r["embedding"],
            "quality": r["quality"],
            "crop_uri": r["crop_uri"],
            "created_at": r["created_at"].isoformat(),
            "sighting_id": r["sighting_id"],  # AC-API-41 link, restored on unmerge
        }
        for entity in (s, t)
        for r in conn.execute(
            """DELETE FROM entity_exemplar WHERE exemplar_id IN (
                   SELECT exemplar_id FROM entity_exemplar WHERE entity_id = %s
                    ORDER BY quality DESC, created_at DESC, exemplar_id DESC OFFSET %s)
               RETURNING exemplar_id, entity_id, embedding::text AS embedding, quality, crop_uri, created_at, sighting_id""",
            (entity, EXEMPLAR_CAP),
        )
    ]

    # The review that resolved this merge (review merge_into) is reopened, so the operator can decide again.
    reopened = None
    review_id = detail.get("review_id")
    previous_resolution = None
    if review_id:
        r = conn.execute(
            """UPDATE review_item r SET status = 'open', resolution = NULL, resolved_by = NULL, resolved_at = NULL
                 FROM (SELECT review_id, resolution, resolved_by, resolved_at FROM review_item
                        WHERE review_id = %s FOR UPDATE) old
                WHERE r.review_id = old.review_id AND r.status = 'resolved'
                  AND r.resolution->>'action' = 'merge_into' AND r.resolution->>'target_entity_id' = %s
            RETURNING r.review_id, old.resolution, old.resolved_by, old.resolved_at""",
            (review_id, t),
        ).fetchone()
        if r is not None:
            reopened = str(r["review_id"])
            previous_resolution = {
                "resolution": r["resolution"],
                "resolved_by": r["resolved_by"],
                "resolved_at": r["resolved_at"].isoformat() if r["resolved_at"] else None,
            }
    restored["reopened_review_id"] = reopened

    # Target fields: put back the pre-merge value only where the field still holds what the merge wrote.
    restored_fields: list[str] = []
    tp_last_seen, tp_first_seen = _ts(tp.get("last_seen_at")), _ts(tp.get("first_seen_at"))
    tp_loc_at = _ts(tp.get("location_set_at"))
    src_last_seen = source["last_seen_at"]
    merge_last_seen = (
        src_last_seen if src_last_seen is not None and (tp_last_seen is None or src_last_seen > tp_last_seen)
        else tp_last_seen
    )
    if target["last_seen_at"] == merge_last_seen and merge_last_seen != tp_last_seen:
        conn.execute("UPDATE entity SET last_seen_at = %s WHERE entity_id = %s", (tp_last_seen, t))
        restored_fields.append("last_seen_at")
    src_loc_at = source["location_set_at"] or source["last_seen_at"]
    tgt_prev_loc_at = tp_loc_at or tp_last_seen
    location_moved = (
        source["last_location"] is not None and src_loc_at is not None
        and (tgt_prev_loc_at is None or src_loc_at > tgt_prev_loc_at)
    )
    if location_moved and (target["last_location"], target["location_set_at"], target["location_set_by"]) == (
        source["last_location"], src_loc_at, source["location_set_by"] or "pipeline"
    ):
        conn.execute(
            "UPDATE entity SET last_location = %s, location_set_at = %s, location_set_by = %s WHERE entity_id = %s",
            (tp.get("last_location"), tp_loc_at, tp.get("location_set_by"), t),
        )
        restored_fields.append("last_location")
    firsts = [v for v in (tp_first_seen, source["first_seen_at"]) if v is not None]
    merge_first_seen = min(firsts) if firsts else None
    if target["first_seen_at"] == merge_first_seen and merge_first_seen != tp_first_seen:
        conn.execute("UPDATE entity SET first_seen_at = %s WHERE entity_id = %s", (tp_first_seen, t))
        restored_fields.append("first_seen_at")
    if restored["review_ids"]:
        still_open = conn.execute(
            "SELECT 1 FROM review_item WHERE entity_id = %s AND status = 'open' LIMIT 1", (t,)
        ).fetchone()
        if still_open is None:
            conn.execute(
                "UPDATE entity SET needs_review = %s, review_reason = %s WHERE entity_id = %s",
                (bool(tp.get("needs_review")), tp.get("review_reason"), t),
            )
            restored_fields.append("needs_review")
    conn.execute("UPDATE entity SET updated_at = now() WHERE entity_id = %s", (t,))
    restored["target_fields"] = restored_fields

    conn.execute(
        """UPDATE entity SET status = %s, merged_into = NULL, needs_review = %s, review_reason = %s, updated_at = now()
            WHERE entity_id = %s""",
        (sp.get("status") or "provisional", bool(sp.get("needs_review")), sp.get("review_reason"), s),
    )

    write_audit(
        conn,
        "split",
        [source_id, target_id],
        actor,
        {
            "action": "unmerge",
            "merge_audit_id": audit["audit_id"],
            "source_entity_id": s,
            "target_entity_id": t,
            "restored": restored,
            "dropped_exemplars": dropped_now,
            "previous_resolution": previous_resolution,
            "note": note,
        },
    )
    return {"target_entity_id": t, "restored": restored}
