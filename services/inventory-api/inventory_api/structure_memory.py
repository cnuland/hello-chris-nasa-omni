"""AC-API-46 structure memory (UI6-05): "Not inventory" dismissals that perception remembers (AC-PER-64).

``record_not_inventory`` runs inside the resolve transaction; ``GET /structure-memory`` lists the active rows.
"""

from __future__ import annotations

import re
from typing import Any
from uuid import UUID

import psycopg
from fastapi import APIRouter, Query

from .deps import Ctx
from .repo import real

router = APIRouter(tags=["review"])

NOT_INVENTORY_NOTE = "not_inventory"
MAX_TOKENS = 40
SIGHTING_DESCRIPTIONS = 5
# words that say nothing about what the thing looks like (articles, prepositions, pronouns, auxiliaries)
STOP_WORDS = frozenset(
    ["the", "and", "with", "for", "from", "that", "this", "these", "those", "are", "was", "were", "its", "into", "onto", "over", "under", "near", "along", "its", "their", "his", "her", "has", "have", "had", "been", "being", "which", "who", "whom", "what", "where", "when", "there", "here", "than", "then", "also", "very", "some", "any", "each", "one", "two", "not", "but", "yet", "just", "only", "seen", "visible", "appears", "appear", "looks", "look"]
)
# review round: nothing about people is remembered (a sighting description can mention a nearby crew member)
PERSON_WORDS = frozenset(
    ["crew", "member", "members", "person", "persons", "people", "astronaut", "astronauts", "cosmonaut", "cosmonauts", "man", "men", "woman", "women", "human", "humans", "face", "faces", "hair", "head", "hand", "hands", "arm", "arms", "wearing", "wears", "worn", "shirt", "shirts", "pants", "trousers", "shorts", "beard", "someone", "somebody"]
)


def is_not_inventory(note: str | None) -> bool:
    return bool(note) and note.strip().lower() == NOT_INVENTORY_NOTE


def label_key(text: str) -> str:
    """Perception's AC-PER-31 key: lower case, every non-alphanumeric character removed."""
    return re.sub(r"[^a-z0-9]+", "", str(text or "").lower())


def tokens(*texts: str | None) -> list[str]:
    """Distinct lower-case words of at least 3 letters (letters only), minus STOP_WORDS, first-seen order, capped."""
    out: list[str] = []
    for text in texts:
        for word in re.findall(r"[a-z]+", str(text or "").lower()):
            if len(word) >= 3 and word not in STOP_WORDS and word not in PERSON_WORDS and word not in out:
                out.append(word)
                if len(out) >= MAX_TOKENS:
                    return out
    return out


def _module_of(conn: psycopg.Connection, location_id: str | None) -> str | None:
    if location_id is None:
        return None
    row = conn.execute(
        """WITH RECURSIVE up(location_id, parent_id, kind, depth) AS (
               SELECT location_id, parent_id, kind, 0 FROM location WHERE location_id = %s
               UNION ALL
               SELECT l.location_id, l.parent_id, l.kind, up.depth + 1
                 FROM location l JOIN up ON l.location_id = up.parent_id WHERE up.depth < 32
           ) SELECT location_id FROM up WHERE kind = 'module' ORDER BY depth LIMIT 1""",
        (location_id,),
    ).fetchone()
    return row["location_id"] if row else None


def record_not_inventory(
    conn: psycopg.Connection, review: dict[str, Any], entity_id: UUID | None, actor: str
) -> int | None:
    """Write the memory row for a not_inventory dismissal of an uncatalogued entity; its id, or None."""
    if entity_id is None:
        return None
    entity = conn.execute(
        "SELECT label, description, catalog_id, last_location FROM entity WHERE entity_id = %s", (entity_id,)
    ).fetchone()
    if entity is None or entity["catalog_id"] is not None:
        return None
    descriptions = [
        r["description"]
        for r in conn.execute(
            "SELECT description FROM sighting WHERE entity_id = %s AND description IS NOT NULL "
            "ORDER BY seen_at DESC, sighting_id DESC LIMIT %s",
            (entity_id, SIGHTING_DESCRIPTIONS),
        )
    ]
    location = review.get("location_id") or entity["last_location"]
    embedding = conn.execute(
        """SELECT x.embedding::text AS embedding FROM entity_exemplar x JOIN sighting s ON s.sighting_id = x.sighting_id
            WHERE x.entity_id = %s AND NOT s.person_overlap
            ORDER BY x.quality DESC, x.created_at DESC, x.exemplar_id DESC LIMIT 1""",
        (entity_id,),
    ).fetchone()
    row = conn.execute(
        """INSERT INTO structure_memory (review_id, entity_id, label, label_key, description_tokens, location_id,
               module_id, embedding, created_by)
           VALUES (%s, %s, %s, %s, %s, %s, %s, %s::halfvec, %s)
           ON CONFLICT (review_id) DO NOTHING RETURNING memory_id""",
        (
            review["review_id"],
            entity_id,
            entity["label"],
            label_key(entity["label"]),
            tokens(entity["label"], entity["description"], *descriptions),
            location,
            _module_of(conn, location),
            embedding["embedding"] if embedding else None,
            actor,
        ),
    ).fetchone()
    return int(row["memory_id"]) if row else None


def _vector(value: Any) -> list[float] | None:
    if value is None:
        return None
    if isinstance(value, str):
        return [float(v) for v in value.strip("[]").split(",") if v.strip()]
    return [float(v) for v in (value.to_list() if hasattr(value, "to_list") else value)]


@router.get("/structure-memory")
def list_structure_memory(ctx: Ctx, limit: int = Query(500, ge=1, le=1000)) -> list[dict[str, Any]]:
    """Active memories, newest first: the entity was deleted, or it is still dismissed."""
    with ctx.db.connection() as conn:
        rows = conn.execute(
            """SELECT m.memory_id, m.review_id, m.entity_id, m.label, m.label_key, m.description_tokens,
                      m.location_id, m.module_id, m.embedding::text AS embedding, m.created_by, m.created_at
                 FROM structure_memory m LEFT JOIN entity e ON e.entity_id = m.entity_id
                WHERE m.entity_id IS NULL OR (e.status = 'dismissed' AND lower(trim(COALESCE((
                      SELECT a.detail->>'note' FROM audit_log a
                       WHERE a.op = 'dismiss' AND a.entity_ids @> ARRAY[m.entity_id]
                       ORDER BY a.at DESC, a.audit_id DESC LIMIT 1), ''))) = 'not_inventory')
                ORDER BY m.created_at DESC, m.memory_id DESC LIMIT %s""",
            (limit,),
        ).fetchall()
    return [
        {
            "memory_id": r["memory_id"],
            "review_id": r["review_id"],
            "entity_id": r["entity_id"],
            "label": r["label"],
            "label_key": r["label_key"],
            "description_tokens": list(r["description_tokens"] or []),
            "location_id": r["location_id"],
            "module_id": r["module_id"],
            "embedding": [real(v) for v in _vector(r["embedding"])] if r["embedding"] is not None else None,
            "created_by": r["created_by"],
            "created_at": r["created_at"],
        }
        for r in rows
    ]
