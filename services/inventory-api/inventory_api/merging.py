"""Entity merge (AC-API-9), shared by POST /items/{id}/merge and review merge_into (AC-API-13)."""

from __future__ import annotations

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
        "review_reason FROM entity WHERE entity_id = ANY(%s::uuid[]) ORDER BY entity_id FOR UPDATE",
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
        }
        for r in conn.execute(
            """DELETE FROM entity_exemplar WHERE exemplar_id IN (
                   SELECT exemplar_id FROM entity_exemplar WHERE entity_id = %s
                    ORDER BY quality DESC, created_at DESC, exemplar_id DESC OFFSET %s)
               RETURNING exemplar_id, embedding::text AS embedding, quality, crop_uri, created_at""",
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
            "UPDATE entity SET last_seen_at = %s, last_location = COALESCE(%s, last_location), updated_at = now() "
            "WHERE entity_id = %s",
            (source["last_seen_at"], source["last_location"], str(target_id)),
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
        "note": note,
    }
    if extra_detail:
        detail.update(extra_detail)
    write_audit(conn, "merge", [source_id, target_id], actor, detail)
    return detail
