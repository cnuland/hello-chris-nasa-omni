"""Shared SQL fragments and row serializers."""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from typing import Any
from uuid import UUID

import psycopg
from psycopg.types.json import Jsonb

from .errors import NotFound

HIDDEN_STATUSES = ("merged", "dismissed")

# Trigram similarity above which a label counts as a fuzzy (typo-tolerant) match for q, e.g. 'hamer' -> 'hammer'.
FUZZY_LABEL_THRESHOLD = 0.4

# Self + all descendants of a location (by hierarchy, never by string prefix).
_LOCATION_SUBTREE_TEMPLATE = """
    WITH RECURSIVE sub(location_id) AS (
        SELECT location_id FROM location WHERE location_id = {placeholder}
        UNION
        SELECT l.location_id FROM location l JOIN sub ON l.parent_id = sub.location_id
    ) SELECT location_id FROM sub
"""
LOCATION_SUBTREE_SQL = _LOCATION_SUBTREE_TEMPLATE.format(placeholder="%s")


def location_subtree_sql(param: str) -> str:
    """Subtree CTE bound to the named parameter ``%(param)s`` (for queries that use a params dict)."""
    if not param.isidentifier():
        raise ValueError(f"invalid parameter name {param!r}")
    return _LOCATION_SUBTREE_TEMPLATE.format(placeholder=f"%({param})s")


ITEM_SELECT = """
    SELECT e.entity_id, e.catalog_id, c.display_name AS catalog_name, e.kind, e.status, e.merged_into, e.label,
           e.description, e.attributes, e.last_location, l.display_name AS last_location_name, e.last_seen_at,
           e.first_seen_at, e.confidence, e.snapshot_uri, e.needs_review, e.review_reason, e.parent_entity,
           e.ims_barcode, e.created_by, e.created_at, e.updated_at, e.location_set_at, e.location_set_by,
           mv.actor_desc AS last_moved_by, mv.occurred_at AS last_moved_at,
           lh.event_type AS lh_event_type, lh.actor_desc AS lh_actor_desc, lh.occurred_at AS lh_occurred_at
      FROM entity e
      LEFT JOIN catalog_item c ON c.catalog_id = e.catalog_id
      LEFT JOIN location l ON l.location_id = e.last_location
      LEFT JOIN LATERAL (
            SELECT ev.actor_desc, ev.occurred_at FROM event ev
             WHERE ev.entity_id = e.entity_id AND ev.event_type = 'moved'
             ORDER BY ev.occurred_at DESC, ev.created_at DESC LIMIT 1
      ) mv ON TRUE
      LEFT JOIN LATERAL (
            SELECT ev.event_type, ev.actor_desc, ev.occurred_at FROM event ev
             WHERE ev.entity_id = e.entity_id AND ev.event_type = ANY('{%(handled)s}'::text[])
               AND NOT (ev.event_type = 'added' AND ev.source = 'operator')
             ORDER BY ev.occurred_at DESC, ev.created_at DESC LIMIT 1
      ) lh ON TRUE
"""

# AC-API-30: event types that count as "handling" an item (operator 'added' = item creation is excluded in the query).
HANDLED_EVENT_TYPES = ("moved", "used", "handed_over", "added", "removed", "stowed", "unstowed", "consumed")
ITEM_SELECT = ITEM_SELECT.replace("%(handled)s", ",".join(HANDLED_EVENT_TYPES))

EVENT_COLUMNS = """event_id, event_type, entity_id, actor_entity, actor_desc, from_location, to_location, occurred_at,
                   confidence, evidence_uri, source, created_at"""

# AC-API-29: events with the entity's label/catalog and the location display names.
EVENT_SELECT = """
    SELECT ev.event_id, ev.event_type, ev.entity_id, ev.actor_entity, ev.actor_desc, ev.from_location,
           ev.to_location, ev.occurred_at, ev.confidence, ev.evidence_uri, ev.source, ev.created_at,
           en.label AS entity_label, en.catalog_id AS entity_catalog_id,
           lf.display_name AS from_location_name, lt.display_name AS to_location_name
      FROM event ev
      LEFT JOIN entity en ON en.entity_id = ev.entity_id
      LEFT JOIN location lf ON lf.location_id = ev.from_location
      LEFT JOIN location lt ON lt.location_id = ev.to_location
"""


def jsonb(value: Any) -> Jsonb:
    return Jsonb(value)


def real(value: Any) -> float | None:
    """REAL columns come back as float32 approximations; round for stable JSON output."""
    return None if value is None else round(float(value), 6)


def serialize_item(row: dict[str, Any], bucket: str, url_for) -> dict[str, Any]:
    return {
        "entity_id": row["entity_id"],
        "catalog_id": row["catalog_id"],
        "catalog_name": row["catalog_name"],
        "kind": row["kind"],
        "status": row["status"],
        "merged_into": row["merged_into"],
        "label": row["label"],
        "description": row["description"],
        "attributes": row["attributes"] or {},
        "last_location": row["last_location"],
        "last_location_name": row["last_location_name"],
        "last_seen_at": row["last_seen_at"],
        "first_seen_at": row["first_seen_at"],
        "confidence": real(row["confidence"]),
        "snapshot_url": url_for(row["snapshot_uri"], bucket),
        "needs_review": row["needs_review"],
        "review_reason": row["review_reason"],
        "parent_entity": row["parent_entity"],
        "ims_barcode": row["ims_barcode"],
        "created_by": row["created_by"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "last_moved_by": row["last_moved_by"],
        "last_moved_at": row["last_moved_at"],
        "last_handled": (
            {
                "event_type": row["lh_event_type"],
                "actor_desc": row["lh_actor_desc"],
                "occurred_at": row["lh_occurred_at"],
            }
            if row["lh_event_type"] is not None
            else None
        ),
        "location_set_at": row["location_set_at"],
        "location_set_by": row["location_set_by"],
    }


def serialize_event(row: dict[str, Any], bucket: str, url_for) -> dict[str, Any]:
    out = {
        "event_id": row["event_id"],
        "event_type": row["event_type"],
        "entity_id": row["entity_id"],
        "actor_entity": row["actor_entity"],
        "actor_desc": row["actor_desc"],
        "from_location": row["from_location"],
        "to_location": row["to_location"],
        "occurred_at": row["occurred_at"],
        "confidence": real(row["confidence"]),
        "evidence_url": url_for(row["evidence_uri"], bucket),
        "source": row["source"],
        "created_at": row["created_at"],
    }
    for key in ("entity_label", "entity_catalog_id", "from_location_name", "to_location_name"):
        if key in row:
            out[key] = row[key]
    return out


def fetch_items(conn: psycopg.Connection, ids: Sequence[UUID | str], bucket: str, url_for) -> list[dict[str, Any]]:
    """Fetch Items by id, preserving the order of ``ids`` (unknown ids are skipped)."""
    if not ids:
        return []
    rows = conn.execute(ITEM_SELECT + " WHERE e.entity_id = ANY(%s::uuid[])", ([str(i) for i in ids],)).fetchall()
    by_id = {str(r["entity_id"]): r for r in rows}
    return [serialize_item(by_id[str(i)], bucket, url_for) for i in ids if str(i) in by_id]


def fetch_item(conn: psycopg.Connection, entity_id: UUID | str, bucket: str, url_for) -> dict[str, Any]:
    items = fetch_items(conn, [entity_id], bucket, url_for)
    if not items:
        raise NotFound(f"entity {entity_id} not found")
    return items[0]


def write_audit(
    conn: psycopg.Connection, op: str, entity_ids: Iterable[UUID | str], actor: str, detail: dict[str, Any]
) -> None:
    conn.execute(
        "INSERT INTO audit_log (op, entity_ids, actor, detail) VALUES (%s, %s::uuid[], %s, %s)",
        (op, [str(e) for e in entity_ids], actor, jsonb(detail)),
    )


def location_exists(conn: psycopg.Connection, location_id: str) -> bool:
    return conn.execute("SELECT 1 FROM location WHERE location_id = %s", (location_id,)).fetchone() is not None


def location_no_capture(conn: psycopg.Connection, location_id: str) -> bool:
    """True when the location or any ancestor is flagged no_capture (privacy is inherited)."""
    row = conn.execute(
        """WITH RECURSIVE up(location_id, parent_id, no_capture, depth) AS (
               SELECT location_id, parent_id, no_capture, 0 FROM location WHERE location_id = %s
               UNION ALL
               SELECT l.location_id, l.parent_id, l.no_capture, up.depth + 1
                 FROM location l JOIN up ON l.location_id = up.parent_id WHERE up.depth < 32
           ) SELECT COALESCE(bool_or(no_capture), FALSE) AS private FROM up""",
        (location_id,),
    ).fetchone()
    return bool(row["private"])


def no_capture_locations(conn: psycopg.Connection) -> set[str]:
    """Every location that is no_capture itself or below a no_capture ancestor (privacy is inherited)."""
    rows = conn.execute(
        """WITH RECURSIVE priv(location_id, depth) AS (
               SELECT location_id, 0 FROM location WHERE no_capture
               UNION
               SELECT l.location_id, p.depth + 1 FROM location l JOIN priv p ON l.parent_id = p.location_id
                WHERE p.depth < 32
           ) SELECT DISTINCT location_id FROM priv"""
    ).fetchall()
    return {r["location_id"] for r in rows}


def catalog_exists(conn: psycopg.Connection, catalog_id: str) -> bool:
    return conn.execute("SELECT 1 FROM catalog_item WHERE catalog_id = %s", (catalog_id,)).fetchone() is not None


def like_pattern(q: str) -> str:
    """Substring ILIKE pattern with LIKE wildcards escaped (so '%' and '_' match literally)."""
    escaped = q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"
