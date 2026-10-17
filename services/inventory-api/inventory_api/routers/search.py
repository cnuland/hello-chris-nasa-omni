"""AC-API-15 full-text search and AC-API-16 stats."""

from __future__ import annotations

from itertools import zip_longest
from typing import Any

from fastapi import APIRouter, Query

from ..deps import Ctx
from ..repo import FUZZY_LABEL_THRESHOLD, HIDDEN_STATUSES, like_pattern
from ..schemas import EntityKind, EntityStatus, ReviewKind

router = APIRouter(tags=["search"])

_WINDOW_DOC = "coalesce(w.scene_summary, '') || ' ' || coalesce(w.transcript, '')"
_HEADLINE_OPTS = 'StartSel="", StopSel="", MaxWords=30, MinWords=10, MaxFragments=2, FragmentDelimiter=" ... "'


@router.get("/search")
def search(
    ctx: Ctx,
    q: str = Query(..., min_length=1, max_length=200),
    limit: int = Query(20, ge=1, le=200),
) -> list[dict[str, Any]]:
    """Entity hits (label/description/catalog substring match) and observation-window hits (English full-text over
    scene_summary + transcript). The two ranked lists are interleaved so both kinds stay visible under ``limit``."""
    q = q.strip()
    if not q:
        return []
    pat = like_pattern(q)
    with ctx.db.connection() as conn:
        entities = conn.execute(
            """SELECT e.entity_id AS id, e.label, e.description, c.display_name AS catalog_name,
                      coalesce(e.last_seen_at, e.created_at) AS time
                 FROM entity e LEFT JOIN catalog_item c ON c.catalog_id = e.catalog_id
                WHERE e.status <> ALL(%(hidden)s)
                  AND (e.label ILIKE %(pat)s OR e.description ILIKE %(pat)s OR c.display_name ILIKE %(pat)s
                       OR EXISTS (SELECT 1 FROM unnest(c.synonyms) syn WHERE syn ILIKE %(pat)s)
                       OR similarity(e.label, %(q)s) > %(fuzzy)s)
                ORDER BY (CASE WHEN e.label ILIKE %(pat)s THEN 1 ELSE 0 END) DESC, similarity(e.label, %(q)s) DESC,
                         e.last_seen_at DESC NULLS LAST, e.entity_id
                LIMIT %(limit)s""",
            {"hidden": list(HIDDEN_STATUSES), "pat": pat, "q": q, "limit": limit, "fuzzy": FUZZY_LABEL_THRESHOLD},
        ).fetchall()
        windows = conn.execute(
            f"""SELECT w.window_id AS id, w.t_start AS time, w.camera_id,
                       ts_headline('english', {_WINDOW_DOC}, query, %(opts)s) AS snippet,
                       ts_rank(to_tsvector('english', {_WINDOW_DOC}), query) AS rank
                  FROM observation_window w, plainto_tsquery('english', %(q)s) query
                 WHERE to_tsvector('english', {_WINDOW_DOC}) @@ query
                 ORDER BY rank DESC, w.t_start DESC
                 LIMIT %(limit)s""",
            {"q": q, "opts": _HEADLINE_OPTS, "limit": limit},
        ).fetchall()
    entity_hits = [
        {
            "type": "entity",
            "id": str(e["id"]),
            "snippet": " - ".join(p for p in (e["label"], e["catalog_name"], e["description"]) if p),
            "time": e["time"],
        }
        for e in entities
    ]
    window_hits = [
        {
            "type": "window",
            "id": str(w["id"]),
            "snippet": (w["snippet"] or "").strip(),
            "time": w["time"],
            "camera_id": w["camera_id"],
        }
        for w in windows
    ]
    merged = [h for pair in zip_longest(entity_hits, window_hits) for h in pair if h is not None]
    return merged[:limit]


@router.get("/stats")
def stats(ctx: Ctx) -> dict[str, dict[str, int]]:
    """Counts by status (all entities), by kind and by module (live entities only, zones rolled up to their module),
    and open review items by kind."""
    hidden = list(HIDDEN_STATUSES)
    with ctx.db.connection() as conn:
        by_status = {
            r["k"]: r["n"] for r in conn.execute("SELECT status AS k, count(*) AS n FROM entity GROUP BY status")
        }
        by_kind = {
            r["k"]: r["n"]
            for r in conn.execute(
                "SELECT kind AS k, count(*) AS n FROM entity WHERE status <> ALL(%s) GROUP BY kind", (hidden,)
            )
        }
        by_module = {
            r["k"]: r["n"]
            for r in conn.execute(
                """WITH RECURSIVE anc(start_id, location_id, parent_id, kind, depth) AS (
                   SELECT location_id, location_id, parent_id, kind, 0 FROM location
                   UNION ALL
                   SELECT a.start_id, l.location_id, l.parent_id, l.kind, a.depth + 1
                     FROM anc a JOIN location l ON l.location_id = a.parent_id
                    WHERE a.kind <> 'module' AND a.depth < 32
               ), module_of AS (
                   SELECT start_id, location_id AS module_id FROM anc WHERE kind = 'module'
               )
               SELECT m.module_id AS k, count(*) AS n
                 FROM entity e JOIN module_of m ON m.start_id = e.last_location
                WHERE e.status <> ALL(%s)
                GROUP BY m.module_id""",
                (hidden,),
            )
        }
        open_reviews = {
            r["k"]: r["n"]
            for r in conn.execute(
                "SELECT kind AS k, count(*) AS n FROM review_item WHERE status = 'open' GROUP BY kind"
            )
        }
    return {
        "by_status": {s: by_status.get(s, 0) for s in EntityStatus.__args__},
        "by_kind": {k: by_kind.get(k, 0) for k in EntityKind.__args__},
        "by_module": by_module,
        "open_reviews_by_kind": {k: open_reviews.get(k, 0) for k in ReviewKind.__args__},
    }
