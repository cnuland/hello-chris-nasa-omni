"""Internal endpoints for the perception reconciler.

AC-API-17 POST /ingest/candidates, AC-API-18/19/23 POST /ingest/window (atomic, privacy-aware, stock upserts).
"""

from __future__ import annotations

import hashlib
import json
import logging
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC
from typing import Any
from uuid import UUID, uuid4

import psycopg
from fastapi import APIRouter
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse

from ..deps import Ctx
from ..errors import Unprocessable
from ..privacy import overlaps_person
from ..repo import HIDDEN_STATUSES, jsonb, real, write_audit
from ..schemas import AssignmentIn, CandidatesRequest, IngestWindow
from ..storage import uri_to_key

# AC-API-41 review round: a withheld crop is deleted from the bucket after commit unless something references it
_CROP_REFERENCED_SQL = """SELECT 1 FROM entity_exemplar WHERE crop_uri = %(u)s
                          UNION ALL SELECT 1 FROM entity WHERE snapshot_uri = %(u)s
                          UNION ALL SELECT 1 FROM review_item WHERE image_uri = %(u)s
                          UNION ALL SELECT 1 FROM sighting WHERE frame_uri = %(u)s LIMIT 1"""

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/ingest", tags=["ingest"])

PIPELINE_ACTOR = "pipeline"
# AC-API-39: stored privacy_reason categories (perception AC-PER-43); anything else from a client -> private_zone
PRIVACY_REASONS = frozenset({"private_zone", "sleep_cue", "privacy_hold"})
# decision subject keys that hold model or speech text; dropped for a private window
SUBJECT_TEXT_KEYS = frozenset({"utterance", "description", "transcript", "scene_summary", "text", "evidence"})
EXEMPLAR_CAP = 20
MAX_MERGE_HOPS = 16


def _vector_literal(values: Iterable[float]) -> str:
    return "[" + ",".join(repr(float(v)) for v in values) + "]"


# ------------------------------------------------------------------ candidates
@router.post("/candidates")
def candidates(body: CandidatesRequest, ctx: Ctx) -> list[dict[str, Any]]:
    """Top-k live entities by MAX cosine similarity over their exemplars (exact scan; one row per entity)."""
    sql = """SELECT e.entity_id, e.catalog_id, e.label, e.kind, e.status, e.last_location, e.last_seen_at,
                    max(1 - (x.embedding <=> %(q)s::halfvec)) AS similarity
               FROM entity_exemplar x JOIN entity e ON e.entity_id = x.entity_id
              WHERE e.status <> ALL(%(hidden)s)"""
    params: dict[str, Any] = {"q": _vector_literal(body.embedding), "hidden": list(HIDDEN_STATUSES), "k": body.k}
    if body.catalog_id:
        sql += " AND e.catalog_id = %(catalog_id)s"
        params["catalog_id"] = body.catalog_id
    sql += " GROUP BY e.entity_id ORDER BY similarity DESC, e.entity_id LIMIT %(k)s"
    with ctx.db.connection() as conn:
        rows = conn.execute(sql, params).fetchall()
    return [
        {
            "entity_id": r["entity_id"],
            "catalog_id": r["catalog_id"],
            "label": r["label"],
            "kind": r["kind"],
            "status": r["status"],
            "similarity": real(r["similarity"]),
            "last_location": r["last_location"],
            "last_seen_at": r["last_seen_at"],
        }
        for r in rows
    ]


# ------------------------------------------------------------------ window
@dataclass
class _Locations:
    parent: dict[str, str | None]
    flagged: dict[str, bool]
    _cache: dict[str, bool] = field(default_factory=dict)
    kind: dict[str, str] = field(default_factory=dict)

    def chain(self, location_id: str | None) -> list[str]:
        """The location and its ancestors, nearest first (cycle-safe)."""
        out: list[str] = []
        current = location_id
        while current is not None and current in self.parent and current not in out:
            out.append(current)
            current = self.parent.get(current)
        return out

    def module_of(self, location_id: str | None) -> str | None:
        return next((loc for loc in self.chain(location_id) if self.kind.get(loc) == "module"), None)

    def exists(self, location_id: str) -> bool:
        return location_id in self.parent

    def no_capture(self, location_id: str | None) -> bool:
        """True when the location or any ancestor is flagged no_capture (privacy is inherited)."""
        if location_id is None or location_id not in self.parent:
            return False
        if location_id in self._cache:
            return self._cache[location_id]
        seen: set[str] = set()
        current: str | None = location_id
        result = False
        while current is not None and current not in seen:
            seen.add(current)
            if self.flagged.get(current):
                result = True
                break
            current = self.parent.get(current)
        self._cache[location_id] = result
        return result


def _load_locations(conn: psycopg.Connection) -> _Locations:
    rows = conn.execute("SELECT location_id, parent_id, no_capture, kind FROM location").fetchall()
    return _Locations(
        parent={r["location_id"]: r["parent_id"] for r in rows},
        flagged={r["location_id"]: r["no_capture"] for r in rows},
        kind={r["location_id"]: r["kind"] for r in rows},
    )


def _downgrade_held(
    conn: psycopg.Connection, locs: _Locations, entity_id: UUID, location_id: str, seen_at: Any, hold_s: float
) -> bool:
    """AC-API-45 (UI5-05): True when a sighting at ``location_id`` (outside any module) must not replace the entity's
    module location: the current last_location is a module or inside one, that module lies in ``location_id`` (the
    sighting is coarser, not elsewhere) and the location was set at most ``hold_s`` seconds before ``seen_at``
    (``COALESCE(location_set_at, last_seen_at)``)."""
    if hold_s <= 0 or locs.module_of(location_id) is not None:
        return False
    row = conn.execute(
        "SELECT last_location, last_seen_at, COALESCE(location_set_at, last_seen_at) AS set_at FROM entity "
        "WHERE entity_id = %s",
        (entity_id,),
    ).fetchone()
    if row is None or row["set_at"] is None:
        return False
    if seen_at.tzinfo is None:  # review round: SightingIn accepts naive timestamps; the DB stores UTC
        seen_at = seen_at.replace(tzinfo=UTC)
    if row["last_seen_at"] is not None and seen_at <= row["last_seen_at"]:
        return False  # the UPDATE will not apply: nothing to hold, nothing to freeze
    module = locs.module_of(row["last_location"])
    if module is None or location_id not in locs.chain(module):
        return False
    if not 0 <= (seen_at - row["set_at"]).total_seconds() <= hold_s:
        return False
    # a legacy row (no location_set_at) would measure the hold from last_seen_at, which every held sighting advances:
    # freeze the provenance time so the hold ends
    conn.execute("UPDATE entity SET location_set_at = %s WHERE entity_id = %s AND location_set_at IS NULL",
                 (row["set_at"], entity_id))
    return True


@dataclass(frozen=True)
class _Survivor:
    entity_id: UUID
    kind: str
    last_location: str | None


def _resolve_existing(conn: psycopg.Connection, ids: set[UUID]) -> dict[UUID, _Survivor]:
    """Map each referenced existing entity id to its live survivor (following merged_into). 422 if unknown.

    The survivors are locked FOR UPDATE (in entity_id order, like merge_entities) and re-checked after the lock: if a
    merge committed while we waited, the chain is resolved again. This serializes ingest against merges, so a new
    sighting can never be attached to an entity that was merged away concurrently.
    """
    if not ids:
        return {}
    for _attempt in range(MAX_MERGE_HOPS):
        rows = conn.execute(
            """WITH RECURSIVE chain(start_id, entity_id, merged_into, status, hops) AS (
                   SELECT entity_id, entity_id, merged_into, status, 0 FROM entity
                    WHERE entity_id = ANY(%s::uuid[])
                   UNION ALL
                   SELECT c.start_id, e.entity_id, e.merged_into, e.status, c.hops + 1
                     FROM chain c JOIN entity e ON e.entity_id = c.merged_into
                    WHERE c.status = 'merged' AND c.hops < %s
               )
               SELECT DISTINCT ON (start_id) start_id, entity_id, status FROM chain ORDER BY start_id, hops DESC""",
            ([str(i) for i in ids], MAX_MERGE_HOPS),
        ).fetchall()
        resolved = {r["start_id"]: r["entity_id"] for r in rows if r["status"] != "merged"}
        missing = ids - set(resolved)
        if missing:
            raise Unprocessable(f"unknown entity_id(s): {', '.join(sorted(str(m) for m in missing))}")
        locked = {
            r["entity_id"]: r
            for r in conn.execute(
                "SELECT entity_id, status, kind, last_location FROM entity WHERE entity_id = ANY(%s::uuid[]) "
                "ORDER BY entity_id FOR UPDATE",
                (sorted(str(v) for v in set(resolved.values())),),
            )
        }
        if all(r["status"] != "merged" for r in locked.values()):
            return {
                start: _Survivor(sid, locked[sid]["kind"], locked[sid]["last_location"])
                for start, sid in resolved.items()
            }
        logger.info("entity merged while ingest waited for its lock; resolving again")
    raise Unprocessable("could not resolve referenced entities (merge chain kept changing)")


def _validate_references(conn: psycopg.Connection, body: IngestWindow, locs: _Locations) -> None:
    if conn.execute("SELECT 1 FROM camera WHERE camera_id = %s", (body.window.camera_id,)).fetchone() is None:
        raise Unprocessable(f"unknown camera_id {body.window.camera_id!r}")
    location_refs: list[tuple[str, str | None]] = []
    for i, a in enumerate(body.assignments):
        location_refs.append((f"assignments[{i}].sighting.location_id", a.sighting.location_id))
    for i, ev in enumerate(body.events):
        location_refs += [
            (f"events[{i}].from_location", ev.from_location),
            (f"events[{i}].to_location", ev.to_location),
        ]
    for i, rv in enumerate(body.reviews):
        location_refs.append((f"reviews[{i}].location_id", rv.location_id))
    for i, su in enumerate(body.stock_updates):
        location_refs.append((f"stock_updates[{i}].location_id", su.location_id))
    for where, loc in location_refs:
        if loc is not None and not locs.exists(loc):
            raise Unprocessable(f"{where}: unknown location {loc!r}")

    catalog_ids = {a.new_entity.catalog_id for a in body.assignments if a.new_entity and a.new_entity.catalog_id}
    catalog_ids |= {su.catalog_id for su in body.stock_updates}
    if catalog_ids:
        found = {
            r["catalog_id"]
            for r in conn.execute(
                "SELECT catalog_id FROM catalog_item WHERE catalog_id = ANY(%s)", (sorted(catalog_ids),)
            )
        }
        if catalog_ids - found:
            raise Unprocessable(f"unknown catalog_id(s): {', '.join(sorted(catalog_ids - found))}")


def _check_uris(body: IngestWindow, bucket: str) -> None:
    """Every image URI must be ``s3://<our bucket>/<safe key>`` so /media can serve it and nothing else leaks out."""
    uris: list[tuple[str, str | None]] = []
    for i, a in enumerate(body.assignments):
        uris.append((f"assignments[{i}].sighting.frame_uri", a.sighting.frame_uri))
        if a.exemplar is not None:
            uris.append((f"assignments[{i}].exemplar.crop_uri", a.exemplar.crop_uri))
    uris += [(f"events[{i}].evidence_uri", ev.evidence_uri) for i, ev in enumerate(body.events)]
    uris += [(f"reviews[{i}].image_uri", rv.image_uri) for i, rv in enumerate(body.reviews)]
    for where, uri in uris:
        if uri is not None and uri_to_key(uri, bucket) is None:
            raise Unprocessable(f"{where}: must be an s3://{bucket}/<key> URI with a safe key")


@dataclass
class _Privacy:
    """AC-API-19 decisions for one payload, made before anything is written."""

    private_assignments: list[bool]
    private_events: list[bool]
    dropped_uris: set[str]


def _target_key(ref: str | None, entity_id: UUID | None, survivors: dict[UUID, _Survivor]) -> str | None:
    if ref is not None:
        return f"ref:{ref}"
    if entity_id is not None:
        return f"id:{survivors[entity_id].entity_id}"
    return None


def _decide_privacy(
    body: IngestWindow,
    locs: _Locations,
    camera_location: str | None,
    survivors: dict[UUID, _Survivor],
    force: bool = False,
) -> _Privacy:
    """Work out what must not be stored, and reject reviews that would need a private image (422).

    - A sighting is private when its location (or, without one, the camera's location) is no_capture.
    - An event is private when from/to, any sighting of its entity in this payload, or (with none of those) the camera
      location is no_capture.
    - A review is private when its location, any sighting of its entity in this payload, the existing entity's stored
      last_location (when this payload does not sight it), or (when it names nothing else) the camera location is
      no_capture; or when its image is one that this payload drops for privacy.
    - ``force`` (AC-API-39: the client flagged the window private): every part is private, and any review is 422.
    """
    sightings_by_target: dict[str, list[str | None]] = {}
    private_assignments: list[bool] = []
    dropped: set[str] = set()
    for a in body.assignments:
        loc = a.sighting.location_id or camera_location
        private = force or locs.no_capture(loc)
        private_assignments.append(private)
        if private:
            dropped |= {u for u in (a.sighting.frame_uri, a.exemplar.crop_uri if a.exemplar else None) if u}
        for key in {_target_key(a.ref, None, survivors), _target_key(None, a.entity_id, survivors)} - {None}:
            sightings_by_target.setdefault(key, []).append(loc)

    private_events: list[bool] = []
    for ev in body.events:
        locations: list[str | None] = [ev.from_location, ev.to_location]
        locations += sightings_by_target.get(_target_key(ev.entity_ref, ev.entity_id, survivors) or "", [])
        if not any(locations):
            locations = [camera_location]
        private = force or any(locs.no_capture(loc) for loc in locations) or (ev.evidence_uri in dropped)
        private_events.append(private)
        if private and ev.evidence_uri:
            dropped.add(ev.evidence_uri)

    for i, rv in enumerate(body.reviews):
        key = _target_key(rv.entity_ref, rv.entity_id, survivors)
        locations = [rv.location_id] + sightings_by_target.get(key or "", [])
        if rv.entity_id is not None and not sightings_by_target.get(key or ""):
            locations.append(survivors[rv.entity_id].last_location)
        if not any(locations):
            locations = [camera_location]
        if force:
            raise Unprocessable(f"reviews[{i}]: the window is private; a review cannot carry an image or description")
        if any(locs.no_capture(loc) for loc in locations):
            raise Unprocessable(f"reviews[{i}]: location is no_capture; a review cannot carry an image there")
        if rv.image_uri in dropped:
            raise Unprocessable(f"reviews[{i}]: image_uri was captured in a no_capture zone and is not stored")
    return _Privacy(private_assignments, private_events, dropped)


def _window_privacy(
    body: IngestWindow, locs: _Locations, camera_location: str | None, privacy: _Privacy
) -> tuple[bool, str | None]:
    """AC-API-39: (is_private, privacy_reason). Private when the client says so (``private`` or the older
    ``privacy.no_capture``), the camera's location is no_capture, or any sighting/event is private (AC-API-19). A
    client cannot make a window capturable that the API sees as private."""
    w = body.window
    claimed = bool(w.private) or bool((w.privacy or {}).get("no_capture"))
    private = (
        claimed
        or locs.no_capture(camera_location)
        or any(privacy.private_assignments)
        or any(privacy.private_events)
    )
    if not private:
        return False, None
    reason = w.privacy_reason or (w.privacy or {}).get("reason")
    return True, reason if reason in PRIVACY_REASONS else "private_zone"


def _person_overlap(a: AssignmentIn) -> bool:
    """AC-API-41: the sighting's crop overlaps a person: the client says so, or it sends person boxes and an exemplar
    whose crop box (the sighting bbox) overlaps one of them, or cannot be checked (no bbox)."""
    s = a.sighting
    if s.person_overlap:
        return True
    if not s.person_boxes or a.exemplar is None:
        return False
    return s.bbox is None or overlaps_person(s.bbox, s.person_boxes, s.person_box_kinds)


def _withheld_crops(
    body: IngestWindow, kinds: list[str], overlaps: list[bool], privacy: _Privacy
) -> dict[str, str | None]:
    """AC-API-41: crop URI -> the frame a review shows instead (None when the sighting has no stored frame), for every
    crop that is not stored because it overlaps a person or belongs to a person entity. A review that would need a
    withheld crop and has no frame to fall back on is 422 (checked before anything is written)."""
    withheld: dict[str, str | None] = {}
    for a, kind, overlap, private in zip(body.assignments, kinds, overlaps, privacy.private_assignments, strict=True):
        crop = a.exemplar.crop_uri if a.exemplar is not None else None
        if crop and (overlap or kind == "person"):
            withheld[crop] = None if private else a.sighting.frame_uri
    for i, rv in enumerate(body.reviews):
        if rv.image_uri in withheld and withheld[rv.image_uri] is None:
            raise Unprocessable(f"reviews[{i}]: image_uri is a crop that overlaps a person and there is no frame to "
                                "show instead")
    return withheld


def _move_within_one_place(ev: Any, locs: _Locations) -> bool:
    """AC-API-47: a pipeline/speech ``moved`` event whose destination is its origin or contains it (LAB1 -> USOS)."""
    if ev.event_type != "moved" or ev.source not in ("pipeline", "speech"):
        return False
    if ev.from_location is None or ev.to_location is None:
        return False
    # review round: only a coarsening (to is from, or contains it); a refinement USOS -> LAB1 is information
    return ev.to_location in locs.chain(ev.from_location)


def _request_digest(body: IngestWindow) -> str:
    canonical = json.dumps(body.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()


def _previous_result(conn: psycopg.Connection, digest: str) -> dict[str, Any] | None:
    """The response of an identical, already committed ingest (a client retry), if any.

    Serialized per digest with a transaction-scoped advisory lock so two concurrent retries cannot both write.
    """
    conn.execute("SELECT pg_advisory_xact_lock(%s)", (int.from_bytes(bytes.fromhex(digest[:16]), "big", signed=True),))
    row = conn.execute(
        """SELECT detail FROM audit_log
            WHERE op = 'update' AND actor = %s AND detail->>'request_sha256' = %s
            ORDER BY audit_id LIMIT 1""",
        (PIPELINE_ACTOR, digest),
    ).fetchone()
    if row is None:
        return None
    return {"window_id": UUID(row["detail"]["window_id"]), "entities": row["detail"].get("entities", {})}


@router.post("/window", status_code=201)
def ingest_window(body: IngestWindow, ctx: Ctx) -> JSONResponse:
    """Record one reconciled observation window atomically. Any invalid part -> 422 and nothing is written."""
    _check_uris(body, ctx.bucket)
    try:
        with ctx.db.connection() as conn, conn.transaction():
            result, withheld = _record_window(conn, body, hold_s=float(ctx.settings.location_downgrade_hold_s))
    except psycopg.IntegrityError as exc:
        logger.warning("ingest rejected by database constraint", extra={"error": exc.diag.message_primary})
        raise Unprocessable(f"constraint violation: {exc.diag.message_primary}") from exc
    except psycopg.DataError as exc:
        logger.warning("ingest rejected: invalid data", extra={"error": exc.diag.message_primary})
        raise Unprocessable(f"invalid data: {exc.diag.message_primary}") from exc
    _purge_withheld(ctx, withheld)
    logger.info(
        "window ingested",
        extra={
            "window_id": str(result["window_id"]),
            "assignments": len(body.assignments),
            "events": len(body.events),
            "reviews": len(body.reviews),
            "stock_updates": len(body.stock_updates),
        },
    )
    return JSONResponse(jsonable_encoder(result), status_code=201)


def _purge_withheld(ctx: Any, withheld: set[str]) -> None:
    """Delete withheld person crops from the object store (perception uploaded them before the API refused them), so
    /media cannot serve them at their guessable key. Best effort: a failure is logged, never fails the ingest."""
    for uri in sorted(withheld):
        key = uri_to_key(uri, ctx.bucket)
        if key is None:
            continue
        try:
            with ctx.db.connection() as conn:
                if conn.execute(_CROP_REFERENCED_SQL, {"u": uri}).fetchone() is not None:
                    continue
            ctx.store.delete(key)
            logger.info("withheld person crop deleted from the object store", extra={"key": key})
        except Exception as exc:  # noqa: BLE001 - object store / DB are remote dependencies
            logger.warning("deleting a withheld crop failed", extra={"key": key, "error": str(exc)})


def _record_window(
    conn: psycopg.Connection, body: IngestWindow, *, hold_s: float = 0.0
) -> tuple[dict[str, Any], set[str]]:
    digest = _request_digest(body)
    previous = _previous_result(conn, digest)
    if previous is not None:
        logger.info(
            "identical ingest already recorded; returning the original result",
            extra={"window_id": str(previous["window_id"])},
        )
        return previous, set()

    locs = _load_locations(conn)
    _validate_references(conn, body, locs)
    referenced = {a.entity_id for a in body.assignments if a.entity_id}
    referenced |= {ev.entity_id for ev in body.events if ev.entity_id}
    referenced |= {rv.entity_id for rv in body.reviews if rv.entity_id}
    referenced |= {c for rv in body.reviews for c in rv.candidate_ids}
    survivors = _resolve_existing(conn, referenced)
    camera_row = conn.execute(
        "SELECT location_id FROM camera WHERE camera_id = %s", (body.window.camera_id,)
    ).fetchone()
    camera_location = camera_row["location_id"] if camera_row else None
    w0 = body.window
    claimed = bool(w0.private) or bool((w0.privacy or {}).get("no_capture")) or locs.no_capture(camera_location)
    privacy = _decide_privacy(body, locs, camera_location, survivors, force=claimed)

    w = body.window
    private_window, privacy_reason = _window_privacy(body, locs, camera_location, privacy)
    kinds = [a.new_entity.kind if a.new_entity is not None else survivors[a.entity_id].kind  # type: ignore[index]
             for a in body.assignments]
    overlaps = [_person_overlap(a) for a in body.assignments]
    withheld = _withheld_crops(body, kinds, overlaps, privacy)
    # AC-API-39: a private window keeps no text, whatever the client sent
    window_id = conn.execute(
        """INSERT INTO observation_window (camera_id, t_start, t_end, scene_summary, transcript, audio_events, model,
               raw_uri, is_private, privacy_reason) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
           RETURNING window_id""",
        (
            w.camera_id,
            w.t_start,
            w.t_end,
            None if private_window else w.scene_summary,
            None if private_window else w.transcript,
            jsonb([] if private_window else w.audio_events),
            w.model,
            w.raw_uri,
            private_window,
            privacy_reason,
        ),
    ).fetchone()["window_id"]

    ref_map: dict[str, UUID] = {}
    created: list[UUID] = []
    updated: list[UUID] = []
    for a, private, kind, overlap in zip(body.assignments, privacy.private_assignments, kinds, overlaps, strict=True):
        s = a.sighting
        # AC-API-41: no crop or exemplar of a person (a crop over a person region, or of a person entity)
        no_exemplar = overlap or kind == "person"
        frame_uri = None if private else s.frame_uri
        crop_uri = None if private or no_exemplar or a.exemplar is None else a.exemplar.crop_uri
        snapshot = crop_uri or frame_uri
        if a.new_entity is not None:
            ne = a.new_entity
            entity_id = uuid4()
            conn.execute(
                """INSERT INTO entity (entity_id, catalog_id, kind, status, label, description, attributes,
                       last_location, last_seen_at, first_seen_at, confidence, snapshot_uri, created_by,
                       location_set_at, location_set_by)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'pipeline', %s, %s)""",
                (
                    entity_id,
                    ne.catalog_id,
                    ne.kind,
                    ne.status,
                    ne.label,
                    None if private_window else ne.description,
                    jsonb(ne.attributes),
                    s.location_id,
                    s.seen_at,
                    s.seen_at,
                    s.confidence,
                    snapshot,
                    s.seen_at if s.location_id is not None else None,
                    PIPELINE_ACTOR if s.location_id is not None else None,
                ),
            )
            write_audit(
                conn,
                "create",
                [entity_id],
                PIPELINE_ACTOR,
                {
                    "window_id": str(window_id),
                    "ref": a.ref,
                    "label": ne.label,
                    "kind": ne.kind,
                    "status": ne.status,
                    "catalog_id": ne.catalog_id,
                },
            )
            created.append(entity_id)
        else:
            assert a.entity_id is not None
            survivor = survivors[a.entity_id]
            entity_id = survivor.entity_id
            new_location = s.location_id
            if new_location is not None and _downgrade_held(conn, locs, entity_id, new_location, s.seen_at, hold_s):
                new_location = None  # AC-API-45: keep the module; the sighting itself stays at segment level
                logger.info("segment sighting keeps the module location (downgrade hold)",
                            extra={"entity_id": str(entity_id), "location_id": s.location_id})
            # AC-API-31: a sighting newer than last_seen_at refreshes last_seen_at/confidence/snapshot. It moves the
            # entity only when it is also newer than location_set_at (an operator's move is not undone by a late
            # window). Both conditions are evaluated on the row before this UPDATE. first_seen_at can move earlier.
            conn.execute(
                """UPDATE entity SET
                       last_location = CASE WHEN %(loc)s::text IS NOT NULL
                                             AND %(seen)s > COALESCE(location_set_at, '-infinity')
                                            THEN %(loc)s ELSE last_location END,
                       location_set_at = CASE WHEN %(loc)s::text IS NOT NULL
                                               AND %(seen)s > COALESCE(location_set_at, '-infinity')
                                              THEN %(seen)s ELSE location_set_at END,
                       location_set_by = CASE WHEN %(loc)s::text IS NOT NULL
                                               AND %(seen)s > COALESCE(location_set_at, '-infinity')
                                              THEN %(by)s ELSE location_set_by END,
                       last_seen_at = %(seen)s, confidence = %(conf)s,
                       snapshot_uri = COALESCE(%(snap)s, snapshot_uri), updated_at = now()
                    WHERE entity_id = %(id)s AND (last_seen_at IS NULL OR last_seen_at < %(seen)s)""",
                {
                    "loc": new_location,
                    "seen": s.seen_at,
                    "conf": s.confidence,
                    "snap": snapshot,
                    "id": entity_id,
                    "by": PIPELINE_ACTOR,
                },
            )
            conn.execute(
                "UPDATE entity SET first_seen_at = LEAST(COALESCE(first_seen_at, %(seen)s), %(seen)s) "
                "WHERE entity_id = %(id)s",
                {"seen": s.seen_at, "id": entity_id},
            )
            if entity_id not in updated:
                updated.append(entity_id)
        if a.ref:
            ref_map[a.ref] = entity_id
        sighting_id = conn.execute(
            """INSERT INTO sighting (entity_id, window_id, location_id, seen_at, bbox, bbox_source, confidence,
                   frame_uri, description, person_overlap) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
               RETURNING sighting_id""",
            (entity_id, window_id, s.location_id, s.seen_at, s.bbox, s.bbox_source, s.confidence, frame_uri,
             None if private_window else s.description, overlap),
        ).fetchone()["sighting_id"]
        # No appearance vector of a person is kept (AC-PER-36 / AC-API-41), and none cut from a crop that overlaps a
        # person. Object embeddings from private windows are kept without their crop, because dedup needs them.
        if a.exemplar is not None and not no_exemplar:
            conn.execute(
                "INSERT INTO entity_exemplar (entity_id, embedding, quality, crop_uri, sighting_id) "
                "VALUES (%s, %s::halfvec, %s, %s, %s)",
                (entity_id, _vector_literal(a.exemplar.embedding), a.exemplar.quality, crop_uri, sighting_id),
            )
            conn.execute(
                """DELETE FROM entity_exemplar WHERE exemplar_id IN (
                       SELECT exemplar_id FROM entity_exemplar WHERE entity_id = %s
                        ORDER BY quality DESC, created_at DESC, exemplar_id DESC OFFSET %s)""",
                (entity_id, EXEMPLAR_CAP),
            )

    def target(ref: str | None, entity_id: UUID | None) -> UUID | None:
        if ref is not None:
            return ref_map[ref]
        return survivors[entity_id].entity_id if entity_id is not None else None

    touched: list[UUID] = created + updated
    event_ids: list[str] = []
    moved_skipped = 0
    for ev, private in zip(body.events, privacy.private_events, strict=True):
        if _move_within_one_place(ev, locs):
            # AC-API-47 (UI6-08): LAB1 -> USOS is a loss of precision, not a move
            moved_skipped += 1
            logger.info("moved event inside one place skipped",
                        extra={"from_location": ev.from_location, "to_location": ev.to_location})
            continue
        ev_entity = target(ev.entity_ref, ev.entity_id)
        event_ids.append(
            str(
                conn.execute(
                    """INSERT INTO event (event_type, entity_id, actor_desc, from_location, to_location, occurred_at,
                           confidence, evidence_uri, source) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                       RETURNING event_id""",
                    (
                        ev.event_type,
                        ev_entity,
                        None if private_window else ev.actor_desc,
                        ev.from_location,
                        ev.to_location,
                        ev.occurred_at,
                        ev.confidence,
                        None if private else ev.evidence_uri,
                        ev.source,
                    ),
                ).fetchone()["event_id"]
            )
        )
        if ev_entity is not None and ev_entity not in touched:
            touched.append(ev_entity)

    review_ids: list[str] = []
    for rv in body.reviews:
        review_entity = target(rv.entity_ref, rv.entity_id)
        review_ids.append(
            str(
                conn.execute(
                    """INSERT INTO review_item (kind, entity_id, candidate_ids, image_uri, description, observed_at,
                           location_id, model_guess, probabilities)
                       VALUES (%s, %s, %s::uuid[], %s, %s, %s, %s, %s, %s) RETURNING review_id""",
                    (
                        rv.kind,
                        review_entity,
                        [str(survivors[c].entity_id) for c in rv.candidate_ids],
                        withheld.get(rv.image_uri) or rv.image_uri,  # AC-API-41: the frame, never a person crop
                        rv.description,
                        rv.observed_at,
                        rv.location_id,
                        rv.model_guess,
                        jsonb(rv.probabilities),
                    ),
                ).fetchone()["review_id"]
            )
        )
        if review_entity is not None:
            conn.execute(
                "UPDATE entity SET needs_review = TRUE, review_reason = %s WHERE entity_id = %s",
                (rv.kind, review_entity),
            )
            if review_entity not in touched:
                touched.append(review_entity)

    for d in body.decisions:
        conn.execute(
            """INSERT INTO decision_log (question, subject, choice, probabilities, model, latency_ms)
               VALUES (%s, %s, %s, %s, %s, %s)""",
            (
                d.question,
                jsonb({k: v for k, v in d.subject.items() if k not in SUBJECT_TEXT_KEYS} if private_window else d.subject),
                d.choice,
                jsonb(d.probabilities),
                d.model,
                d.latency_ms,
            ),
        )

    for su in body.stock_updates:
        conn.execute(
            """INSERT INTO stock (catalog_id, location_id, est_count, last_seen_at) VALUES (%s, %s, %s, %s)
               ON CONFLICT (catalog_id, location_id) DO UPDATE
                  SET est_count = EXCLUDED.est_count, last_seen_at = EXCLUDED.last_seen_at
                WHERE stock.last_seen_at < EXCLUDED.last_seen_at""",
            (su.catalog_id, su.location_id, su.est_count, su.seen_at),
        )

    entities = {ref: str(eid) for ref, eid in ref_map.items()}
    # AC-API-21: every ingest is attributed, also when it only updates existing entities or stock. The digest makes
    # an identical retry return this result instead of writing everything twice.
    write_audit(
        conn,
        "update",
        touched,
        PIPELINE_ACTOR,
        {
            "window_id": str(window_id),
            "request_sha256": digest,
            "entities": entities,
            "created_entity_ids": [str(e) for e in created],
            "updated_entity_ids": [str(e) for e in updated],
            "event_ids": event_ids,
            "review_ids": review_ids,
            "decisions": len(body.decisions),
            "stock_keys": [{"catalog_id": su.catalog_id, "location_id": su.location_id} for su in body.stock_updates],
            "moved_events_skipped": moved_skipped,
            "privacy_dropped_uris": len(privacy.dropped_uris),
            "person_crops_withheld": len(withheld),
            "private": private_window,
        },
    )
    return {"window_id": window_id, "entities": entities}, set(withheld)
