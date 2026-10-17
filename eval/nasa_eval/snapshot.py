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


@dataclass(frozen=True)
class PredSighting:
    sighting_id: str
    entity_id: str
    seen_at: datetime
    location_id: str | None = None
    bbox: tuple[float, float, float, float] | None = None


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


@dataclass
class Snapshot:
    entities: dict[str, PredEntity] = field(default_factory=dict)  # non-merged entities only
    sightings: list[PredSighting] = field(default_factory=list)  # entity_id resolved through merged_into
    events: list[PredEvent] = field(default_factory=list)  # entity_id resolved through merged_into
    reviews: list[PredReview] = field(default_factory=list)
    locations: dict[str, tuple[str | None, str]] = field(default_factory=dict)  # id -> (parent_id, kind)


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
            "SELECT entity_id::text, catalog_id, status, kind, last_location, last_seen_at, merged_into::text "
            "FROM entity ORDER BY created_at, entity_id"
        ).fetchall()
        merged_into = {r[0]: r[6] for r in rows if r[2] == "merged"}
        resolve = _resolver(merged_into)
        for eid, catalog_id, status, kind, last_location, last_seen_at, _ in rows:
            if status == "merged":
                continue
            snap.entities[eid] = PredEntity(eid, catalog_id, status, kind, last_location, last_seen_at)
        for sid, eid, seen_at, loc, bbox in conn.execute(
            "SELECT sighting_id::text, entity_id::text, seen_at, location_id, bbox FROM sighting "
            "ORDER BY seen_at, sighting_id"
        ):
            snap.sightings.append(PredSighting(sid, resolve(eid), seen_at, loc, _bbox(bbox)))
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
            }
            for s in snap.sightings
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
        )
    for s in d.get("sightings", []):
        snap.sightings.append(
            PredSighting(
                s["sighting_id"], s["entity_id"], _parse_ts(s["seen_at"]), s.get("location_id"), _bbox(s.get("bbox"))
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
