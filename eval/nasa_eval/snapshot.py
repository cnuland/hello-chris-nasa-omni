"""Read the pipeline's result out of an inventory database (used by the runner, AC-EVAL-5)."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class PredEntity:
    entity_id: str
    catalog_id: str | None
    status: str
    kind: str = "object"
    last_location: str | None = None
    last_seen_at: datetime | None = None
    description: str | None = None  # person tracks: the appearance descriptor (perception AC-PER-35)


@dataclass(frozen=True)
class PredSighting:
    sighting_id: str
    entity_id: str
    seen_at: datetime
    location_id: str | None = None
    bbox: tuple[float, float, float, float] | None = None
    window_id: str | None = None
    frame_uri: str | None = None


@dataclass(frozen=True)
class PredWindow:
    """An observation window as stored (perception AC-PER-37 privacy gate: ``raw_uri`` must be null when private)."""

    window_id: str
    t_start: datetime
    t_end: datetime
    raw_uri: str | None = None
    scene_summary: str | None = None


@dataclass(frozen=True)
class PredEvent:
    event_type: str
    entity_id: str
    occurred_at: datetime
    actor_desc: str | None = None
    to_location: str | None = None


@dataclass(frozen=True)
class PredReview:
    kind: str
    entity_id: str | None
    status: str = "open"


@dataclass(frozen=True)
class RevisitResult:
    """The scene replayed a second time after ``gap_s`` (perception AC-PER-32): ``new_entity_ids`` = entities the
    revisit created, ``objects`` = gold objects of the scene (the denominator of ``revisit_new_entity_rate``)."""

    gap_s: float
    objects: int
    new_entity_ids: list[str] = field(default_factory=list)


@dataclass
class Snapshot:
    entities: dict[str, PredEntity] = field(default_factory=dict)  # non-merged entities only
    sightings: list[PredSighting] = field(default_factory=list)  # entity_id resolved through merged_into
    events: list[PredEvent] = field(default_factory=list)  # entity_id resolved through merged_into
    reviews: list[PredReview] = field(default_factory=list)
    locations: dict[str, tuple[str | None, str]] = field(default_factory=dict)  # id -> (parent_id, kind)
    windows: list[PredWindow] = field(default_factory=list)
    revisit: RevisitResult | None = None  # recorded by the runner (or the reference snapshot); not in the DB


def _resolver(merged_into: dict[str, str | None]):
    """Return a function following ``merged_into`` chains to the live (non-merged) entity id."""

    def resolve(eid: str) -> str:
        seen: set[str] = set()
        while merged_into.get(eid) and eid not in seen:
            seen.add(eid)
            eid = merged_into[eid]  # type: ignore[assignment]
        return eid

    return resolve


def _bbox(value: list[float] | None) -> tuple[float, float, float, float] | None:
    if value is None or len(value) != 4:
        return None
    return tuple(round(float(v), 6) for v in value)  # type: ignore[return-value]


def read_snapshot(database_url: str) -> Snapshot:
    """Read entities, sightings, events, reviews and the location tree from an inventory database
    (contracts/db/001_init.sql). Sightings / events / reviews of merged entities are re-attributed to the entity they
    were merged into, so the snapshot reflects the pipeline's final identity decisions."""
    import psycopg

    snap = Snapshot()
    with psycopg.connect(database_url) as conn:
        for loc_id, parent, kind in conn.execute("SELECT location_id, parent_id, kind FROM location"):
            snap.locations[loc_id] = (parent, kind)
        rows = conn.execute(
            "SELECT entity_id::text, catalog_id, status, kind, last_location, last_seen_at, merged_into::text, "
            "description FROM entity ORDER BY created_at, entity_id"
        ).fetchall()
        merged_into = {r[0]: r[6] for r in rows if r[2] == "merged"}
        resolve = _resolver(merged_into)
        for eid, catalog_id, status, kind, last_location, last_seen_at, _, description in rows:
            if status == "merged":
                continue
            snap.entities[eid] = PredEntity(eid, catalog_id, status, kind, last_location, last_seen_at, description)
        for sid, eid, seen_at, loc, bbox, wid, frame_uri in conn.execute(
            "SELECT sighting_id::text, entity_id::text, seen_at, location_id, bbox, window_id::text, frame_uri "
            "FROM sighting ORDER BY seen_at, sighting_id"
        ):
            snap.sightings.append(PredSighting(sid, resolve(eid), seen_at, loc, _bbox(bbox), wid, frame_uri))
        for wid, t_start, t_end, raw_uri, summary in conn.execute(
            "SELECT window_id::text, t_start, t_end, raw_uri, scene_summary FROM observation_window "
            "ORDER BY t_start, window_id"
        ):
            snap.windows.append(PredWindow(wid, t_start, t_end, raw_uri, summary))
        for etype, eid, occurred_at, actor, to_loc in conn.execute(
            "SELECT event_type, entity_id::text, occurred_at, actor_desc, to_location FROM event "
            "ORDER BY occurred_at, event_id"
        ):
            snap.events.append(PredEvent(etype, resolve(eid), occurred_at, actor, to_loc))
        for kind, eid, status in conn.execute(
            "SELECT kind, entity_id::text, status FROM review_item ORDER BY created_at, review_id"
        ):
            snap.reviews.append(PredReview(kind, resolve(eid) if eid else None, status))
    log.info(
        "snapshot entities=%d sightings=%d events=%d reviews=%d",
        len(snap.entities),
        len(snap.sightings),
        len(snap.events),
        len(snap.reviews),
    )
    return snap


# ------------------------------------------------------------------------------------------------------- JSON
SNAPSHOT_FORMAT = "nasa-eval-snapshot/1"


def _ts(value: datetime | None) -> str | None:
    return None if value is None else value.isoformat()


def _parse_ts(value: str | None) -> datetime | None:
    return None if value is None else datetime.fromisoformat(value)


def snapshot_to_dict(snap: Snapshot, **meta: object) -> dict:
    """JSON-safe dict of a Snapshot (``meta`` keys, e.g. source / recorded_at, are stored alongside)."""
    return {
        "format": SNAPSHOT_FORMAT,
        **meta,
        "locations": {k: list(v) for k, v in sorted(snap.locations.items())},
        "entities": [
            {
                "entity_id": e.entity_id,
                "catalog_id": e.catalog_id,
                "status": e.status,
                "kind": e.kind,
                "last_location": e.last_location,
                "last_seen_at": _ts(e.last_seen_at),
                **({} if e.description is None else {"description": e.description}),
            }
            for e in snap.entities.values()
        ],
        "sightings": [
            {
                "sighting_id": s.sighting_id,
                "entity_id": s.entity_id,
                "seen_at": _ts(s.seen_at),
                "location_id": s.location_id,
                "bbox": None if s.bbox is None else list(s.bbox),
                **({} if s.window_id is None else {"window_id": s.window_id}),
                **({} if s.frame_uri is None else {"frame_uri": s.frame_uri}),
            }
            for s in snap.sightings
        ],
        "windows": [
            {
                "window_id": w.window_id,
                "t_start": _ts(w.t_start),
                "t_end": _ts(w.t_end),
                "raw_uri": w.raw_uri,
                "scene_summary": w.scene_summary,
            }
            for w in snap.windows
        ],
        "events": [
            {
                "event_type": e.event_type,
                "entity_id": e.entity_id,
                "occurred_at": _ts(e.occurred_at),
                "actor_desc": e.actor_desc,
                "to_location": e.to_location,
            }
            for e in snap.events
        ],
        "reviews": [{"kind": r.kind, "entity_id": r.entity_id, "status": r.status} for r in snap.reviews],
        **(
            {}
            if snap.revisit is None
            else {
                "revisit": {
                    "gap_s": snap.revisit.gap_s,
                    "objects": snap.revisit.objects,
                    "new_entity_ids": list(snap.revisit.new_entity_ids),
                }
            }
        ),
    }


def snapshot_from_dict(d: dict) -> Snapshot:
    """Inverse of snapshot_to_dict. Raises ValueError on an unknown format."""
    if d.get("format") != SNAPSHOT_FORMAT:
        raise ValueError(f"not a {SNAPSHOT_FORMAT} snapshot (format={d.get('format')!r})")
    snap = Snapshot(locations={k: (v[0], v[1]) for k, v in d.get("locations", {}).items()})
    for e in d.get("entities", []):
        snap.entities[e["entity_id"]] = PredEntity(
            e["entity_id"],
            e.get("catalog_id"),
            e.get("status", "provisional"),
            e.get("kind", "object"),
            e.get("last_location"),
            _parse_ts(e.get("last_seen_at")),
            e.get("description"),
        )
    for s in d.get("sightings", []):
        snap.sightings.append(
            PredSighting(
                s["sighting_id"],
                s["entity_id"],
                _parse_ts(s["seen_at"]),
                s.get("location_id"),
                _bbox(s.get("bbox")),
                s.get("window_id"),
                s.get("frame_uri"),
            )
        )
    for e in d.get("events", []):
        snap.events.append(
            PredEvent(
                e["event_type"], e["entity_id"], _parse_ts(e["occurred_at"]), e.get("actor_desc"), e.get("to_location")
            )
        )
    for r in d.get("reviews", []):
        snap.reviews.append(PredReview(r["kind"], r.get("entity_id"), r.get("status", "open")))
    for w in d.get("windows", []):
        snap.windows.append(
            PredWindow(
                w["window_id"], _parse_ts(w["t_start"]), _parse_ts(w["t_end"]), w.get("raw_uri"), w.get("scene_summary")
            )
        )
    rv = d.get("revisit")
    if isinstance(rv, dict):
        snap.revisit = RevisitResult(
            float(rv["gap_s"]), int(rv["objects"]), [str(e) for e in rv.get("new_entity_ids") or []]
        )
    return snap


def save_snapshot_json(snap: Snapshot, path, **meta: object) -> None:
    import json
    from pathlib import Path

    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(snapshot_to_dict(snap, **meta), indent=1, sort_keys=False) + "\n")


def load_snapshot_json(path) -> Snapshot:
    import json
    from pathlib import Path

    return snapshot_from_dict(json.loads(Path(path).read_text()))
