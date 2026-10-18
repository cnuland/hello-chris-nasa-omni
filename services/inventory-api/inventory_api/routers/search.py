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
    scene_summary + transcript; private windows never, AC-API-39). The two ranked lists are interleaved so both kinds stay visible under ``limit``."""
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
                 WHERE NOT w.is_private AND to_tsvector('english', {_WINDOW_DOC}) @@ query
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
            "is_private": False,  # AC-API-39: private windows are never returned; the flag is explicit for clients
        }
        for w in windows
    ]
    merged = [h for pair in zip_longest(entity_hits, window_hits) for h in pair if h is not None]
    return merged[:limit]


def _location_levels(rows: list[dict[str, Any]]) -> dict[str, tuple[str, str]]:
    """AC-API-43: location_id -> (level, key). level "module" with the module id (zones roll up), or "segment" with
    the segment id for a place at segment level (or below a segment but outside any module); places with neither
    are left out (they count as unknown)."""
    parent = {r["location_id"]: r["parent_id"] for r in rows}
    kind = {r["location_id"]: r["kind"] for r in rows}
    out: dict[str, tuple[str, str]] = {}
    for loc in parent:
        current, seen, segment = loc, set(), None
        while current is not None and current not in seen:
            seen.add(current)
            if kind.get(current) == "module":
                out[loc] = ("module", current)
                break
            if kind.get(current) == "segment" and segment is None:
                segment = current
            current = parent.get(current)
        else:
            if segment is not None:
                out[loc] = ("segment", segment)
    return out


@router.get("/stats")
def stats(ctx: Ctx) -> dict[str, dict[str, int]]:
    """Counts by status (all entities), by kind and by module (live entities only, zones rolled up to their module;
    items placed only at a segment, e.g. USOS, under the segment's own key: perception could not place them inside a
    module), live entities by location level (AC-API-43), open review items by kind, and observation windows (total and
    private, AC-API-39: a count reveals nothing)."""
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
        levels = _location_levels(conn.execute("SELECT location_id, parent_id, kind FROM location").fetchall())
        by_location = conn.execute(
            "SELECT last_location AS k, count(*) AS n FROM entity WHERE status <> ALL(%s) GROUP BY last_location",
            (hidden,),
        ).fetchall()
        open_reviews = {
            r["k"]: r["n"]
            for r in conn.execute(
                "SELECT kind AS k, count(*) AS n FROM review_item WHERE status = 'open' GROUP BY kind"
            )
        }
        windows = conn.execute(
            "SELECT count(*) AS total, count(*) FILTER (WHERE is_private) AS private FROM observation_window"
        ).fetchone()
    by_module: dict[str, int] = {}
    by_level = {"module": 0, "segment": 0, "unknown": 0}
    for r in by_location:
        level, key = levels.get(r["k"], ("unknown", None)) if r["k"] is not None else ("unknown", None)
        by_level[level] += r["n"]
        if key is not None:
            by_module[key] = by_module.get(key, 0) + r["n"]
    return {
        "windows": {"total": windows["total"], "private": windows["private"]},
        "by_status": {s: by_status.get(s, 0) for s in EntityStatus.__args__},
        "by_kind": {k: by_kind.get(k, 0) for k in EntityKind.__args__},
        "by_module": by_module,
        "by_location_level": by_level,
        "open_reviews_by_kind": {k: open_reviews.get(k, 0) for k in ReviewKind.__args__},
    }
