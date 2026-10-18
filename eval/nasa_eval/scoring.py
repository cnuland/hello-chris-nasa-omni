"""Score a pipeline run (Snapshot) against a gold Dataset: produces the flat metrics dict used by reports and gates.

Pipeline:
1. ``assign_sightings``: each predicted object sighting goes to its best compatible gold appearance (same time
   within ``match_time_tolerance_s``, IoU >= threshold when both boxes exist). Many sightings may go to one
   appearance, so two entities sighted on the same box in the same frame both count (a central dedup failure is
   visible). The default tolerance is one perception window span (AC-PER-2: sightings may be stamped per window).
2. A gold appearance is *covered* when a sighting assigned to the same gold object lies within the tolerance.
   Entity-resolution items = every predicted object sighting (gold label from its assignment, or none = spurious)
   plus every uncovered gold appearance (no predicted label = missed). Pairwise / B-cubed run over all of them,
   so misses and spurious sightings are penalised (``metrics`` pads the missing side with singletons).
3. Predicted entities are assigned to gold objects by plurality (``metrics.assign_entities``); locations, events and
   unknown-recall are scored through that assignment.
4. Location at audit time: the gold location comes from appearances AND events up to ``audit_t`` (a ``removed`` /
   ``consumed`` object is out of scope and leaves the denominator); the predicted location comes from the
   pipeline's sightings / events up to ``audit_t`` (its final ``last_location`` only applies once ``audit_t`` is at
   or after the entity's ``last_seen_at``).
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from datetime import datetime
from typing import Any

from nasa_eval import metrics as m
from nasa_eval.dataset import Dataset, GoldObject
from nasa_eval.metrics import DEFAULT_EVENT_TOLERANCE_S
from nasa_eval.snapshot import PredSighting, RevisitResult, Snapshot

log = logging.getLogger(__name__)

METRIC_KEYS = (
    "detection_macro_f1",
    "pairwise_precision",
    "pairwise_recall",
    "pairwise_f1",
    "bcubed_precision",
    "bcubed_recall",
    "bcubed_f1",
    "duplicate_rate",
    "false_merge_rate",
    "sighting_recall",
    "object_recall",
    "spurious_entity_rate",
    "location_acc",
    "location_module_acc",
    "event_precision",
    "event_recall",
    "event_f1",
    "actor_accuracy",
    "unknown_recall",
    "revisit_new_entity_rate",
    "person_track_recall",
    "person_tracks_per_person",
    "sleep_window_media",
    "sleep_window_person_sightings",
)
UNKNOWN_CLASS = "unknown"
DETECTION_IOU = 0.5
MATCH_TIME_TOLERANCE_S = 0.5  # one-to-one ``match_sightings`` (per-frame)
# Scoring default = the time span of one perception window (AC-PER-2: 6 frames at 1 fps = 5 s, stride 4 s): a
# sighting stamped with its window's time can be up to one window span away from the frame the object was seen in.
DEFAULT_MATCH_TOLERANCE_S = 5.0
MATCH_IOU = 0.3
OUT_OF_SCOPE_EVENTS = ("removed", "consumed")
REMOVED = "<removed>"  # predicted "location" of an entity the pipeline says was removed


def appearance_key(gold_object_id: str, index: int) -> str:
    return f"{gold_object_id}@{index}"


def _class_of(catalog_id: str | None) -> str:
    return catalog_id if catalog_id else UNKNOWN_CLASS


def _rel(ts: datetime, start: datetime) -> float:
    return (ts - start).total_seconds()


def gold_locations_at(gold: Iterable[GoldObject], t_audit: float) -> dict[str, str]:
    """Gold object id -> where it is at ``t_audit``, from its appearances and events with t <= t_audit.

    The latest piece of evidence wins (an event wins a tie with an appearance): an appearance gives its location,
    an event with ``to_location`` gives that, a ``removed`` / ``consumed`` event takes the object out of scope.
    Objects not yet seen and objects out of scope are omitted (they are not in the location denominator)."""
    out: dict[str, str] = {}
    for g in gold:
        evidence: list[tuple[float, int, str | None]] = [
            (a.t, 0, a.location) for a in g.appearances if a.t <= t_audit + 1e-9
        ]
        for e in g.events:
            if e.t > t_audit + 1e-9:
                continue
            if e.type in OUT_OF_SCOPE_EVENTS:
                evidence.append((e.t, 1, None))
            elif e.to_location:
                evidence.append((e.t, 1, e.to_location))
        if evidence:
            loc = max(evidence, key=lambda x: (x[0], x[1]))[2]
            if loc is not None:
                out[g.gold_object_id] = loc
    return out


def match_sightings(
    sightings: Iterable[PredSighting],
    dataset: Dataset,
    *,
    time_tolerance_s: float = MATCH_TIME_TOLERANCE_S,
    iou_threshold: float = MATCH_IOU,
) -> dict[str, str]:
    """Predicted sighting id -> gold appearance key (one-to-one).

    Sightings are processed in the given order; each takes the best still-free gold appearance within
    ``time_tolerance_s`` (highest IoU, then same location, then smallest time difference). When both boxes exist the
    IoU must reach ``iou_threshold``; a sighting or appearance without a box matches on time alone."""
    start = dataset.manifest.start_time
    apps = [(appearance_key(g.gold_object_id, i), a) for g in dataset.gold for i, a in enumerate(g.appearances)]
    taken: set[str] = set()
    out: dict[str, str] = {}
    for s in sightings:
        t = _rel(s.seen_at, start)
        best: tuple[tuple[float, int, float], str] | None = None
        for key, a in apps:
            if key in taken:
                continue
            dt = abs(a.t - t)
            if dt > time_tolerance_s + 1e-9:
                continue
            if s.bbox is not None and a.bbox is not None:
                ov = m.iou(s.bbox, a.bbox)
                if ov + 1e-9 < iou_threshold:
                    continue
            else:
                ov = 0.0
            rank = (ov, int(s.location_id == a.location), -dt)
            if best is None or rank > best[0]:
                best = (rank, key)
        if best is not None:
            taken.add(best[1])
            out[s.sighting_id] = best[1]
    return out


def assign_sightings(
    sightings: Iterable[PredSighting],
    dataset: Dataset,
    *,
    time_tolerance_s: float = DEFAULT_MATCH_TOLERANCE_S,
    iou_threshold: float = MATCH_IOU,
) -> dict[str, str]:
    """Predicted sighting id -> best compatible gold appearance key (many-to-one).

    Ranking among appearances within ``time_tolerance_s``: highest IoU, then same location, then smallest time
    difference. When both boxes exist the IoU must reach ``iou_threshold``; without a box only time counts."""
    start = dataset.manifest.start_time
    apps = [(appearance_key(g.gold_object_id, i), a) for g in dataset.gold for i, a in enumerate(g.appearances)]
    out: dict[str, str] = {}
    for s in sightings:
        t = _rel(s.seen_at, start)
        best: tuple[tuple[float, int, float], str] | None = None
        for key, a in apps:
            dt = abs(a.t - t)
            if dt > time_tolerance_s + 1e-9:
                continue
            if s.bbox is not None and a.bbox is not None:
                ov = m.iou(s.bbox, a.bbox)
                if ov + 1e-9 < iou_threshold:
                    continue
            else:
                ov = 0.0
            rank = (ov, int(s.location_id == a.location), -dt)
            if best is None or rank > best[0]:
                best = (rank, key)
        if best is not None:
            out[s.sighting_id] = best[1]
    return out


def _predicted_locations_at(
    snapshot: Snapshot,
    object_sightings: list[PredSighting],
    entity_to_gold: dict[str, str],
    t_audit: float,
    start: datetime,
) -> dict[str, str | None]:
    """Gold object id -> the pipeline's location for it at ``t_audit`` (latest evidence over its entities)."""
    evidence: dict[str, tuple[float, int, str | None]] = {}

    def offer(gid: str, item: tuple[float, int, str | None]) -> None:
        if item[0] <= t_audit + 1e-9 and (gid not in evidence or item[:2] > evidence[gid][:2]):
            evidence[gid] = item

    last_seen: dict[str, float] = {}
    for s in object_sightings:
        gid = entity_to_gold.get(s.entity_id)
        t = _rel(s.seen_at, start)
        last_seen[s.entity_id] = max(last_seen.get(s.entity_id, t), t)
        if gid is not None and s.location_id:
            offer(gid, (t, 0, s.location_id))
    for eid, gid in entity_to_gold.items():
        ent = snapshot.entities.get(eid)
        if ent is None or not ent.last_location:
            continue
        t_final = _rel(ent.last_seen_at, start) if ent.last_seen_at is not None else last_seen.get(eid)
        if t_final is not None:
            offer(gid, (t_final, 1, ent.last_location))  # final state beats a sighting at the same time
    for e in snapshot.events:
        gid = entity_to_gold.get(e.entity_id)
        if gid is None:
            continue
        if e.event_type in OUT_OF_SCOPE_EVENTS:
            offer(gid, (_rel(e.occurred_at, start), 2, REMOVED))
        elif e.to_location:
            offer(gid, (_rel(e.occurred_at, start), 2, e.to_location))
    return {gid: item[2] for gid, item in evidence.items()}


def revisit_new_entity_rate(revisit: RevisitResult | None) -> float | None:
    """Entities created when the scene was replayed after a long gap / gold objects of the scene (perception
    AC-PER-32). None when no revisit was measured, which the gate treats as a failure."""
    if revisit is None:
        return None
    return len(revisit.new_entity_ids) / revisit.objects if revisit.objects else 0.0


# ------------------------------------------------------------------------------------------------------- people
# perception AC-PER-35..38: words that say nothing about which crew member it is
GENERIC_PERSON_WORDS = frozenset(
    {"crew", "member", "person", "astronaut", "a", "an", "the", "in", "with", "wearing", "and"}
)
# GoldOmni's sleep-cue window carries this neutral summary (no sleep cue: only people[] can reveal it); summaries are
# stored for private windows too (perception AC-PER-11), so the scorer finds the window by it.
SLEEP_SUMMARY = "Crew member resting in the module"


def _words(text: str | None) -> set[str]:
    import re

    return set(re.findall(r"[a-z0-9]+", str(text or "").lower()))


def gold_people(dataset: Dataset) -> list[str]:
    """Distinct actor descriptors of the gold events (the people present in the scene)."""
    return sorted({e.actor for g in dataset.gold for e in g.events if e.actor})


def _person_entities(snapshot: Snapshot) -> list[Any]:
    return [e for e in snapshot.entities.values() if e.kind == "person"]


def person_track_recall(dataset: Dataset, snapshot: Snapshot) -> float:
    """Gold actors with a person track whose description contains the actor's descriptor words (everything but
    generic words: "crew member, blue shirt" -> {blue, shirt}) / gold actors; 1.0 when the scene has no actor."""
    actors = gold_people(dataset)
    if not actors:
        return 1.0
    descriptions = [_words(e.description) for e in _person_entities(snapshot)]
    tracked = [a for a in actors if any((_words(a) - GENERIC_PERSON_WORDS) <= d for d in descriptions)]
    return len(tracked) / len(actors)


def person_tracks_per_person(dataset: Dataset, snapshot: Snapshot) -> float:
    """Person entities / gold actors (over-splitting); 0.0 for a scene without actors and without person tracks."""
    actors = gold_people(dataset)
    n = len(_person_entities(snapshot))
    if not actors:
        return float(n)
    return n / len(actors)


def _sleep_window_ids(snapshot: Snapshot) -> set[str]:
    return {w.window_id for w in snapshot.windows if w.scene_summary == SLEEP_SUMMARY}


def sleep_window_person_sightings(dataset: Dataset, snapshot: Snapshot) -> int | None:
    """Person sightings recorded in the sleep-cue window(s): nobody is tracked inside a private zone (perception
    AC-PER-36); None when no such window was recorded."""
    ids = _sleep_window_ids(snapshot)
    if not ids:
        return None
    return sum(
        1
        for s in snapshot.sightings
        if s.window_id in ids and (e := snapshot.entities.get(s.entity_id)) is not None and e.kind == "person"
    )


def sleep_window_media(dataset: Dataset, snapshot: Snapshot) -> int | None:
    """Stored raw replies + sighting frames of the sleep-cue window(s) (scene summary ``SLEEP_SUMMARY``); None when no
    such window was recorded (the gate then fails: never a silent pass)."""
    ids = _sleep_window_ids(snapshot)
    if not ids:
        return None
    raws = sum(1 for w in snapshot.windows if w.window_id in ids and w.raw_uri)
    frames = sum(1 for s in snapshot.sightings if s.window_id in ids and s.frame_uri)
    return raws + frames


def score(
    dataset: Dataset,
    snapshot: Snapshot,
    *,
    audit_t: float | None = None,
    event_tolerance_s: float = DEFAULT_EVENT_TOLERANCE_S,
    match_time_tolerance_s: float = DEFAULT_MATCH_TOLERANCE_S,
) -> dict[str, Any]:
    """Return {"metrics": {METRIC_KEYS...}, "per_class": {catalog_id: {precision, recall, f1}}, "counts": {...},
    "entity_to_gold": {entity_id: gold_object_id}, "audit_t": float}."""
    start = dataset.manifest.start_time
    tol = float(match_time_tolerance_s)
    # person tracks are actors, not inventory items: they are excluded from item scoring
    object_sightings = [
        s for s in snapshot.sightings if (e := snapshot.entities.get(s.entity_id)) is None or e.kind == "object"
    ]

    # 1. detection / class P R F1 per catalog class (one-to-one: every predicted sighting vs every gold appearance)
    pred_det = [
        m.Detection(
            _rel(s.seen_at, start),
            _class_of(e.catalog_id if (e := snapshot.entities.get(s.entity_id)) else None),
            s.bbox,
        )
        for s in object_sightings
    ]
    gold_det = [m.Detection(a.t, _class_of(g.catalog_id), a.bbox) for g in dataset.gold for a in g.appearances]
    per_class = m.detection_prf(pred_det, gold_det, iou_threshold=DETECTION_IOU, time_tolerance_s=tol)

    # 2. entity resolution over ALL predicted sightings and ALL gold appearances
    assigned = assign_sightings(object_sightings, dataset, time_tolerance_s=tol)
    app_object = {
        appearance_key(g.gold_object_id, i): g.gold_object_id for g in dataset.gold for i in range(len(g.appearances))
    }
    seen_times: dict[str, list[float]] = {}
    for s in object_sightings:
        if s.sighting_id in assigned:
            seen_times.setdefault(app_object[assigned[s.sighting_id]], []).append(_rel(s.seen_at, start))
    covered: set[str] = set()
    for g in dataset.gold:
        times = seen_times.get(g.gold_object_id, [])
        for i, a in enumerate(g.appearances):
            if any(abs(a.t - t) <= tol + 1e-9 for t in times):
                covered.add(appearance_key(g.gold_object_id, i))
    pred_map: dict[str, str] = {f"s:{s.sighting_id}": s.entity_id for s in object_sightings}
    gold_map: dict[str, str] = {f"s:{sid}": app_object[key] for sid, key in assigned.items()}
    gold_map.update({f"a:{key}": gid for key, gid in app_object.items() if key not in covered})
    pw = m.pairwise_prf(pred_map, gold_map)
    b3 = m.bcubed_prf(pred_map, gold_map)
    entity_to_gold: dict[str, str] = {str(k): str(v) for k, v in m.assign_entities(pred_map, gold_map).items()}
    objects_found = set(entity_to_gold.values())

    # 3. location top-1 at audit time
    if audit_t is None:
        audit_t = max(
            [a.t for g in dataset.gold for a in g.appearances] + [e.t for g in dataset.gold for e in g.events],
            default=0.0,
        )
    gold_loc = gold_locations_at(dataset.gold, audit_t)
    pred_loc = _predicted_locations_at(snapshot, object_sightings, entity_to_gold, audit_t, start)
    loc = m.location_accuracy(pred_loc, gold_loc, snapshot.locations)

    # 4. events (type + entity + |dt| <= tolerance) and actor attribution
    gold_events = [m.EventRecord(e.type, g.gold_object_id, e.t, e.actor) for g in dataset.gold for e in g.events]
    pred_events = [
        m.EventRecord(
            e.event_type,
            entity_to_gold.get(e.entity_id, f"entity:{e.entity_id}"),
            _rel(e.occurred_at, start),
            e.actor_desc,
        )
        for e in snapshot.events
    ]
    ev = m.event_prf(pred_events, gold_events, tolerance_s=event_tolerance_s)

    # 5. unknown-detection recall: gold unknowns whose entity reached the review queue as unknown_object
    gold_unknowns = [g.gold_object_id for g in dataset.gold if g.is_unknown]
    reviewed = {
        entity_to_gold[r.entity_id]
        for r in snapshot.reviews
        if r.kind == "unknown_object" and r.entity_id in entity_to_gold
    }

    n_apps = len(app_object)
    pred_entities = {s.entity_id for s in object_sightings}
    metrics: dict[str, Any] = {
        "detection_macro_f1": m.macro_f1(per_class),
        "pairwise_precision": pw.precision,
        "pairwise_recall": pw.recall,
        "pairwise_f1": pw.f1,
        "bcubed_precision": b3.precision,
        "bcubed_recall": b3.recall,
        "bcubed_f1": b3.f1,
        "duplicate_rate": m.duplicate_rate(pred_map, gold_map),
        "false_merge_rate": m.false_merge_rate(pred_map, gold_map),
        "sighting_recall": len(covered) / n_apps if n_apps else 1.0,
        "object_recall": len(objects_found) / len(dataset.gold) if dataset.gold else 1.0,
        "spurious_entity_rate": m.spurious_entity_rate(pred_map, gold_map),
        "location_acc": loc.zone,
        "location_module_acc": loc.module,
        "event_precision": ev.precision,
        "event_recall": ev.recall,
        "event_f1": ev.f1,
        "actor_accuracy": ev.actor_accuracy,
        "unknown_recall": m.unknown_recall(gold_unknowns, reviewed),
        "revisit_new_entity_rate": revisit_new_entity_rate(snapshot.revisit),
        "person_track_recall": person_track_recall(dataset, snapshot),
        "person_tracks_per_person": person_tracks_per_person(dataset, snapshot),
        "sleep_window_media": sleep_window_media(dataset, snapshot),
        "sleep_window_person_sightings": sleep_window_person_sightings(dataset, snapshot),
    }
    counts = {
        "gold_objects": len(dataset.gold),
        "gold_appearances": n_apps,
        "gold_events": len(gold_events),
        "gold_unknowns": len(gold_unknowns),
        "pred_entities": sum(1 for e in snapshot.entities.values() if e.kind == "object"),
        "pred_sightings": len(object_sightings),
        "matched_sightings": len(assigned),
        "covered_appearances": len(covered),
        "matched_entities": len(entity_to_gold),
        "spurious_entities": len(pred_entities - set(entity_to_gold)),
        "pred_events": len(pred_events),
        "event_tp": ev.tp,
        "event_fp": ev.fp,
        "event_fn": ev.fn,
        "unknown_reviews": len(reviewed),
        "location_scored": loc.n,
        "revisit_new_entities": None if snapshot.revisit is None else len(snapshot.revisit.new_entity_ids),
        "gold_people": len(gold_people(dataset)),
        "person_tracks": len(_person_entities(snapshot)),
    }
    log.info(
        "scored dataset=%s pairwise_f1=%.3f sighting_recall=%.3f duplicate_rate=%.3f location_acc=%.3f "
        "unknown_recall=%.3f",
        dataset.name,
        pw.f1,
        metrics["sighting_recall"],
        metrics["duplicate_rate"],
        loc.zone,
        metrics["unknown_recall"],
    )
    return {
        "metrics": metrics,
        "per_class": {
            c: {"precision": r.precision, "recall": r.recall, "f1": r.f1, "tp": r.tp, "fp": r.fp, "fn": r.fn}
            for c, r in per_class.items()
        },
        "counts": counts,
        "entity_to_gold": dict(sorted(entity_to_gold.items())),
        "audit_t": audit_t,
        "match_time_tolerance_s": tol,
    }
