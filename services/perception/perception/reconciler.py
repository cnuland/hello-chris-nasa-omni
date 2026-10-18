"""AC-PER-8..11, 13, 16 reconciler: turns one window (omni observation + detector boxes) into an /ingest/window
payload and posts it through the InventoryClient.

Per window, the omni objects are first consolidated (AC-PER-19): same class (or same normalised UNKNOWN label),
not fungible, and omni boxes with IoU >= ``config.consolidate_iou`` (or a missing box, unless both are listed for the
same frame) are ONE instance. Leader clustering by omni confidence, never transitive. Real models list an object once
per frame; without this one window created six laptops.

Per observed instance:
  * link it to a detector box in frame ``frame_index``: first by IoU >= ``config.link_iou`` (all instances), then
    by mapped label (remaining instances); a consolidated instance tries every member's frame and box and keeps the
    best link (detector-linked, box agrees, detector score, omni confidence, later frame);
  * map the label to a catalog class (CatalogMapper); fungible classes -> ``stock_updates`` (count per zone = max
    over the window's frames); UNKNOWN -> ``unknown`` entity + ``unknown_object`` review (crop image, description,
    timestamp);
  * exemplar quality gate (AC-PER-22): only a detector-linked instance whose crop (cut from the full-resolution
    keyframe, ``Keyframe.hires``, AC-PER-21) has a short side >= ``config.min_crop_px`` is embedded and gets an
    exemplar and a stored crop. Otherwise the sighting is recorded without exemplar and matched by class and place
    (AC-PER-22/29): live entities of the class (UNKNOWN: unclassified, same ``label_key``, AC-PER-31) last seen in
    the same module or its zones, one GET /items per (class, module) per window. Any seen within
    ``config.no_exemplar_match_s`` -> the most recent; else (a revisit after any gap) one -> attach, several -> the
    decision model ("same_or_new" with descriptions, last places and times), falling back to the same-zone / most
    recent candidate + ``low_confidence`` review. Outside modules (a segment) the horizon still applies;
  * embed the crop, retrieve candidates (``api.candidates(embedding, catalog_id=..., location_id=...)``);
  * hard gates: class mismatch; impossible transit (different module within ``min_transit_s`` for a non-held
    object); one entity per window (best match keeps it);
  * score ``p = scorer(CandidateFeatures)``; bands: p >= assign_p assign; ask_p <= p < assign_p ask the decision
    model (question "same_or_new", options = candidate entity ids + "NEW"), assign if its top probability >=
    decision_accept, else new provisional entity + ``possible_duplicate`` review; p < ask_p new provisional entity.
    The decision state carries each candidate's description (GET /items, AC-PER-30), last place and ``seen_ago_s``;
  * a catalog class whose live entity count (api.count_live_entities + new ones in this payload) would exceed
    ``expected_count`` -> ``possible_duplicate`` review.
Every decision-model call is logged in ``payload["decisions"]``. Invalid omni results are never written.

Location (AC-PER-11/23, ``LocationResolver.window_policy``): a resolvable hint sets the window location; when a
fixed camera's hint names another module, sighting confidence is capped at ``config.hint_conflict_confidence``.

Privacy (AC-PER-11/23) fails closed (``LocationResolver.window_policy``): a camera without a configured location, a
no_capture camera (whatever the untrusted model hint says) or a no_capture hinted location, or a hint naming a private place ("crew quarters", "WHC", ...) makes the window
private.
In a private window nothing is uploaded (no frames, crops or raw response), no review is created (a review needs an
image), unknown objects are skipped (they could only be resolved from an image), the transcript is not stored, the
utterances are not copied into the decision log and people get no appearance exemplar. Known objects are still
sighted, without image URIs, so inventory locations stay correct.

Speech claims (AC-PER-10/16): placement statements in the transcript that name a catalog item and a destination become
``moved`` events with source ``speech`` (confidence <= ``speech_max_confidence``) on an existing entity, and the
entity is flagged ``needs_review`` (PATCH). The claim state lives in inventory, not in this process, so any camera
process (or a restarted one) can confirm it: when a window sights an entity that is flagged without a
``review_reason`` (i.e. by a speech claim, not by an open review item), its latest speech ``moved`` event is read
from ``GET /events``. A sighting inside the claimed location within ``speech_confirm_s`` clears the flag; a sighting
after that horizon adds a ``conflict`` review (with the sighting's image) so an operator has something to resolve.

Omni events are deduplicated across overlapping windows: the same (entity, event type, destination) within
``event_dedup_s`` is recorded once.
"""

from __future__ import annotations

import json
import logging
import math
import time
from collections import Counter, deque
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

import numpy as np

from . import NEW, UNKNOWN
from .config import PerceptionConfig
from .labels import (
    CatalogMapper,
    is_fixed_in_place,
    is_generic_label,
    is_structure_label,
    label_key,
    normalise_label,
    noun_phrase,
)
from .locations import LocationResolver, ModuleCarry, caption_scene_change
from .media import crop as crop_image
from .media import encode_jpeg
from .people import (
    appearance_closeness,
    appearance_conflict,
    appearance_descriptors,
    appearance_evidence,
    appearance_similarity,
    find_sleep_cue,
    sanitise_text,
)
from .privacy import is_body_label, mentions_person, ordered, overlaps_person, region_kind
from .speech import classify_utterance_logged, parse_speech_claim, split_utterances
from .storage import crop_key, frame_key, raw_key
from .types import DecisionResult, Detection, LabelMatch, OmniResult, Window

logger = logging.getLogger(__name__)

SAME_OR_NEW = "same_or_new"
SPEECH_ACT = "speech_act"
# AC-PER-43 window.privacy_reason categories (the cue text itself is never sent)
PRIVATE_ZONE, SLEEP_CUE, PRIVACY_HOLD = "private_zone", "sleep_cue", "privacy_hold"
MAX_DECISION_OPTIONS = 4  # candidates offered to the decision model (+ NEW)
# Candidates whose match score is within this of the best one are a near tie (identical twins: the same appearance
# against both entities); among them the candidate last seen in the observation's own location ranks first.
NEAR_TIE_P = 0.02
LOW_CONFIDENCE = "low_confidence"
PLACE_ITEMS_LIMIT = 50  # AC-PER-29: same-class entities of one module (GET /items?catalog_id=&location=)
PERSON_RECENT_DESCRIPTIONS = 5  # review round (AC-PER-45): a track's latest sighting appearances kept in memory
UNKNOWN_PLACE_ITEMS_LIMIT = 200  # UNKNOWN has no class filter: every entity of the module is listed
MAX_PERSON_BOXES = 10  # AC-PER-49: person regions sent with a sighting (inventory-api checks crops itself)
#: AC-PER-54 extra detector call for unknown objects: ``query_detect(frame_index, query_texts) -> detections``
QueryDetect = Callable[[int, Sequence[str]], Sequence[Detection]]
LEAVES_INVENTORY = frozenset({"removed", "consumed"})  # AC-PER-51: no destination is filled for these


def structure_filtered(objects: Sequence[dict[str, Any]], mapper: CatalogMapper) -> list[dict[str, Any]]:
    """AC-PER-50: the omni objects that are station structure or an outside view. Only labels that map to UNKNOWN are
    checked, so a catalogued item is always kept however structural it sounds. AC-PER-59: an unknown whose label or
    description says it is fixed in place ("mounted on the wall") is structure too."""
    out = []
    for o in objects:
        label = str(o.get("label") or "")
        if mapper.map(label).catalog_id != UNKNOWN:
            continue
        if is_structure_label(label) or is_fixed_in_place(label) or is_fixed_in_place(str(o.get("description") or "")):
            out.append(o)
    return out


@dataclass(frozen=True)
class CandidateFeatures:
    similarity: float
    class_match: bool
    zone_match: bool
    recency_s: float | None  # seconds between the candidate's last_seen_at and this observation (None = unknown)


# Weights of the match score. Appearance dominates; class/zone/recency move a candidate between bands.
W_SIMILARITY = 0.70
W_CLASS = 0.15
W_ZONE = 0.10
W_RECENCY = 0.05
RECENCY_SCALE_S = 3600.0


def score_candidate(features: CandidateFeatures) -> float:
    """p = f(similarity, class match, zone match, recency) in [0, 1]; monotonic non-decreasing in similarity, class
    match, zone match and recency (more recent -> not lower)."""
    sim = features.similarity
    sim = 0.0 if sim is None or not math.isfinite(sim) else min(max(float(sim), 0.0), 1.0)
    recency = 0.0
    if features.recency_s is not None and math.isfinite(features.recency_s):
        recency = math.exp(-max(float(features.recency_s), 0.0) / RECENCY_SCALE_S)
    p = (
        W_SIMILARITY * sim
        + W_CLASS * float(bool(features.class_match))
        + W_ZONE * float(bool(features.zone_match))
        + W_RECENCY * recency
    )
    return min(max(p, 0.0), 1.0)


def _rank(scored: list[_Scored], location: str | None) -> list[_Scored]:
    """Best first: by score, but candidates within NEAR_TIE_P of the best score are ordered location-first (last
    seen at ``location``), so identical twins keep their zones; remaining ties by entity id (deterministic)."""
    by_p = sorted(scored, key=lambda s: (-s.p, s.entity_id))
    if not by_p or location is None:
        return by_p
    best = by_p[0].p
    near = [s for s in by_p if best - s.p <= NEAR_TIE_P]
    rest = by_p[len(near) :]
    near.sort(key=lambda s: (s.candidate.get("last_location") != location, -s.p, s.entity_id))
    return near + rest


def band(p: float, assign_p: float = 0.85, ask_p: float = 0.5) -> str:
    """ "assign" (p >= assign_p), "ask" (ask_p <= p < assign_p) or "new" (p < ask_p)."""
    if p >= assign_p:
        return "assign"
    if p >= ask_p:
        return "ask"
    return "new"


def iou(a: Sequence[float], b: Sequence[float]) -> float:
    """Intersection over union of two (x1, y1, x2, y2) boxes; 0.0 for degenerate boxes."""
    ax1, ay1, ax2, ay2 = (float(v) for v in a)
    bx1, by1, bx2, by2 = (float(v) for v in b)
    area_a = max(ax2 - ax1, 0.0) * max(ay2 - ay1, 0.0)
    area_b = max(bx2 - bx1, 0.0) * max(by2 - by1, 0.0)
    if area_a <= 0.0 or area_b <= 0.0:
        return 0.0
    inter = max(min(ax2, bx2) - max(ax1, bx1), 0.0) * max(min(ay2, by2) - max(ay1, by1), 0.0)
    union = area_a + area_b - inter
    return inter / union if union > 0 else 0.0


@dataclass
class WindowOutcome:
    """``status``: "posted", "invalid" (omni result invalid/error: nothing written) or "error" (post failed)."""

    status: str
    payload: dict[str, Any] | None = None
    response: dict[str, Any] | None = None
    error: str | None = None


# ---------------------------------------------------------------------------------------------- internals
def _iso(ts: datetime) -> str:
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=UTC)
    return ts.isoformat()


def _parse_ts(value: Any) -> datetime | None:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    try:
        ts = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    return ts if ts.tzinfo else ts.replace(tzinfo=UTC)


def _seen_ago_s(now: datetime, last_seen: Any) -> float | None:
    ts = _parse_ts(last_seen)
    return None if ts is None else round((now - ts).total_seconds(), 1)


def _clamp01(value: Any, default: float = 0.5) -> float:
    try:
        v = float(value)
    except (TypeError, ValueError):
        return default
    if not math.isfinite(v):
        return default
    return min(max(v, 0.0), 1.0)


@dataclass
class _Scored:
    candidate: dict[str, Any]
    p: float

    @property
    def entity_id(self) -> str:
        return str(self.candidate["entity_id"])


@dataclass
class _Observed:
    """One omni object of the window, after linking and label mapping."""

    index: int
    obj: dict[str, Any]
    match: LabelMatch
    frame_idx: int
    bbox: tuple[float, float, float, float] | None
    detection: Detection | None
    held: bool
    ref: str
    location: str | None = None  # camera zone containing the box centre, else the window location
    embedding: np.ndarray | None = None
    scored: list[_Scored] = field(default_factory=list)
    # decision outcome
    entity_id: str | None = None
    previous_location: str | None = None
    reviews: list[dict[str, Any]] = field(default_factory=list)
    clear_claim: str | None = None  # claimed location confirmed by this sighting -> clear the speech flag
    members: list[int] = field(default_factory=list)  # omni object indexes consolidated into this instance
    candidate_ids: set[str] = field(default_factory=set)  # entities compared by appearance (have exemplars)
    person_overlap: bool = False  # AC-PER-49: its crop overlapped a person region and was dropped
    held_cross_module: list[_Scored] = field(default_factory=list)  # AC-PER-60: ask-band candidates in another module

    @property
    def catalog_id(self) -> str | None:
        return None if self.match.catalog_id == UNKNOWN else self.match.catalog_id

    @property
    def is_unknown(self) -> bool:
        return self.match.catalog_id == UNKNOWN


@dataclass
class _Entry:
    """One omni object of the window before consolidation."""

    index: int
    obj: dict[str, Any]
    match: LabelMatch
    frame_index: int | None
    box: tuple[float, float, float, float] | None

    @property
    def confidence(self) -> float:
        return _clamp01(self.obj.get("confidence"), 0.5)


@dataclass
class _Link:
    entry: _Entry
    frame_idx: int
    det: tuple[int, int] | None = None  # (frame, detection index)
    detection: Detection | None = None
    agrees: bool = False  # linked by IoU with omni's box

    @property
    def rank(self) -> tuple:
        return (
            self.detection is not None,
            self.agrees,
            self.detection.score if self.detection else 0.0,
            self.entry.confidence,
            self.frame_idx,
        )


class _WindowContext:
    """Per-window state: location, privacy, storage uploads (cached), decision log."""

    def __init__(
        self, reconciler: Reconciler, window: Window, location: str | None, no_capture: bool, hint_conflict=False
    ):
        self.r = reconciler
        self.window = window
        self.location = location
        self.no_capture = no_capture
        self.hint_conflict = hint_conflict
        self.truncated = False  # AC-PER-26: the omni observation was salvaged from a cut-off reply
        self.carried = False  # AC-PER-41: the window location was carried from an earlier window
        self._frame_uris: dict[int, str | None] = {}
        self._crops: dict[int, str | None] = {}
        self.decisions: list[dict[str, Any]] = []
        # per-window inventory reads (AC-PER-29/30): one query per (class key, place) / class, live count per class
        self.place_items: dict[tuple[tuple[str, str], str], list[dict[str, Any]] | None] = {}
        self.class_items: dict[str, dict[str, dict[str, Any]]] = {}
        self.live_counts: dict[str, int] = {}
        # AC-PER-49: person regions per frame index: (normalised box, "person" | "face")
        self.person_regions: dict[int, list[tuple[tuple[float, float, float, float], str]]] = {}
        self.person_crops_dropped = 0
        # AC-PER-54/55 window statistics of the extra unknown-object queries
        self.unknown_queries = 0
        self.unknown_boxes_linked = 0
        self.unknown_query_person_rejected = 0
        self.unknown_query_ms = 0
        self.generic_unknowns_skipped = 0  # AC-PER-59
        self.unknowns_capped = 0  # AC-PER-59

    def regions(self, idx: int) -> list[tuple[tuple[float, float, float, float], str]]:
        return self.person_regions.get(idx, [])

    def frame_ts(self, idx: int) -> datetime:
        return self.window.frames[idx].timestamp

    def frame_uri(self, idx: int) -> str | None:
        if self.no_capture or not self.window.frames:
            return None
        if idx not in self._frame_uris:
            frame = self.window.frames[idx]
            key = frame_key(self.r.camera_id, frame.timestamp)
            self._frame_uris[idx] = self.r.storage.put(key, encode_jpeg(frame.hires), "image/jpeg")
        return self._frame_uris[idx]

    def crop_uri(self, observed: _Observed, image: np.ndarray) -> str | None:
        if self.no_capture:
            return None
        if observed.index not in self._crops:
            key = crop_key(self.r.camera_id, self.frame_ts(observed.frame_idx), self.r.next_crop_number())
            self._crops[observed.index] = self.r.storage.put(key, encode_jpeg(image), "image/jpeg")
        return self._crops[observed.index]

    def log_decision(self, question: str, subject: dict[str, Any], result: DecisionResult) -> None:
        model = result.model or getattr(self.r.decision, "model", "") or "unknown"
        self.decisions.append(
            {
                "question": question,
                "subject": subject,
                "choice": result.choice or NEW,
                "probabilities": {str(k): float(v) for k, v in result.probabilities.items()},
                "model": str(model),
                "latency_ms": max(int(result.latency_ms or 0), 0),
            }
        )


class Reconciler:
    def __init__(
        self,
        *,
        api: Any,
        embedder: Any,
        decision: Any,
        storage: Any,
        config: PerceptionConfig,
        camera_id: str,
        catalog: Sequence[dict[str, Any]] | None = None,
        locations: Sequence[dict[str, Any]] | None = None,
        text_embedder: Any = None,
        scorer: Callable[[CandidateFeatures], float] | None = None,
        clock: Callable[[], float] | None = None,
    ):
        """``catalog``/``locations`` default to ``api.get_catalog()`` / ``api.get_locations()``; both are reloaded
        from the API every ``config.catalog_refresh_s`` seconds of ``clock`` (default ``time.monotonic``), so review
        resolutions that add catalog classes are used without a restart. ``scorer`` defaults to
        ``score_candidate``. Cross-window state kept here: recently recorded events (dedup) only."""
        self.api = api
        self.embedder = embedder
        self.decision = decision
        self.storage = storage
        self.config = config
        self.camera_id = camera_id
        self.text_embedder = text_embedder
        self.scorer = scorer or score_candidate
        self.clock = clock or time.monotonic
        self.catalog_version = 0
        self._set_catalog(
            list(catalog) if catalog is not None else list(api.get_catalog()),
            list(locations) if locations is not None else list(api.get_locations()),
        )
        self._crop_seq = 0
        # (entity_id, event_type, to_location) -> occurred_at of the last recorded omni event (overlap dedup)
        self._recent_events: dict[tuple[str, str, str | None], datetime] = {}
        # review round (AC-PER-45): appearance texts of each person track's latest sightings (this process only;
        # the same text is stored as the sighting description), so a clash with any of them blocks a match
        self._person_recent: dict[str, deque[str]] = {}
        # AC-PER-37 review: windows starting before this stay private (overlapping windows show the same scene)
        self._private_until: datetime | None = None
        # AC-PER-41: a roaming camera's last own module (resolver and horizon are refreshed per window)
        self._module_carry = ModuleCarry(self.resolver, self.config.module_carry_s)

    def _set_catalog(self, catalog: list[dict[str, Any]], locations: list[dict[str, Any]]) -> None:
        self.catalog = catalog
        self.locations = locations
        self.mapper = CatalogMapper(self.catalog, text_embedder=self.text_embedder, floor=self.config.label_floor)
        self.resolver = LocationResolver(self.locations)
        self._loaded_at = self.clock()
        self.catalog_version += 1

    def refresh_if_stale(self) -> bool:
        """Reload catalog and locations when older than ``config.catalog_refresh_s``; True when reloaded. A failed
        reload keeps the current copy (and retries after the next interval)."""
        ttl = self.config.catalog_refresh_s
        if not ttl or self.clock() - self._loaded_at < ttl:
            return False
        try:
            catalog, locations = list(self.api.get_catalog()), list(self.api.get_locations())
        except Exception as exc:  # noqa: BLE001 - inventory-api is a remote dependency
            logger.warning("catalog refresh failed; keeping the current catalog", extra={"error": str(exc)})
            self._loaded_at = self.clock()
            return False
        if not catalog or not locations:
            self._loaded_at = self.clock()
            return False
        self._set_catalog(catalog, locations)
        logger.info("catalog refreshed", extra={"classes": len(catalog), "locations": len(locations)})
        return True

    def next_crop_number(self) -> int:
        self._crop_seq += 1
        return self._crop_seq

    # ------------------------------------------------------------------ public
    def process(
        self,
        window: Window,
        omni: OmniResult,
        detections: Sequence[Sequence[Detection]],
        *,
        transcript: str | None = None,
        model: str | None = None,
        query_detect: QueryDetect | None = None,
    ) -> WindowOutcome:
        """Reconcile and post one window. ``detections[i]`` are the detector boxes of ``window.frames[i]``.
        ``transcript`` is the final (cleaned) transcript to store and mine for speech claims; None -> the omni
        transcript. ``query_detect(frame_index, texts)`` (AC-PER-54) runs the detector on that frame with extra text
        queries for unknown objects; None -> no extra queries."""
        self.refresh_if_stale()
        if not omni.ok or not isinstance(omni.observation, dict):
            logger.info(
                "omni result not usable; window not written",
                extra={"window": window.index, "status": omni.status, "error": omni.error},
            )
            return WindowOutcome(status="invalid", error=omni.error or f"omni status {omni.status}")
        try:
            payload, observed, claims = self._build(window, omni, detections, transcript, model, query_detect)
        except Exception as exc:
            logger.exception("reconciliation failed", extra={"window": window.index})
            return WindowOutcome(status="error", error=f"{type(exc).__name__}: {exc}")
        try:
            response = self.api.post_window(payload) or {}
        except Exception as exc:  # noqa: BLE001 - InventoryError, transport errors, ...
            logger.warning("posting window failed", extra={"window": window.index, "error": str(exc)})
            return WindowOutcome(status="error", payload=payload, error=f"{type(exc).__name__}: {exc}")
        self._after_post(window, payload, response, observed, claims)
        logger.info(
            "window posted",
            extra={
                "window": window.index,
                "assignments": len(payload["assignments"]),
                "events": len(payload.get("events", [])),
                "reviews": len(payload.get("reviews", [])),
            },
        )
        return WindowOutcome(status="posted", payload=payload, response=response)

    # ------------------------------------------------------------------ building the payload
    def _build(
        self,
        window: Window,
        omni: OmniResult,
        detections: Sequence[Sequence[Detection]],
        transcript: str | None,
        model: str | None,
        query_detect: QueryDetect | None = None,
    ):
        obs = omni.observation or {}
        if transcript is None:
            transcript = str(obs.get("transcript") or "")
        camera_location = self.config.camera_locations.get(self.camera_id)
        scope = self.resolver.window_policy(
            camera_location, obs.get("location_hint"), str(obs.get("scene_summary") or "")  # AC-PER-39/40
        )
        location, no_capture = scope.location, scope.no_capture
        # AC-PER-43: one coarse category per private window ("private_zone" | "sleep_cue" | "privacy_hold")
        privacy: dict[str, Any] | None = {"no_capture": True, "reason": PRIVATE_ZONE} if no_capture else None
        if not no_capture and self.resolver.names_private_place(str(obs.get("scene_summary") or "")):
            # AC-PER-23 review: the hint may name only the parent module while the summary says crew quarters
            logger.info("scene summary names a private place; window treated as no_capture",
                        extra={"camera_id": self.camera_id, "window": window.index})
            no_capture = True
            privacy = {"no_capture": True, "reason": PRIVATE_ZONE}
        if not no_capture and (cue := find_sleep_cue(obs)):
            # AC-PER-37: a sleeping person or a sleeping bag in use means crew quarters (fail closed, over-private)
            logger.info("sleep cue; window treated as no_capture",
                        extra={"camera_id": self.camera_id, "window": window.index, "cue": cue[0], "field": cue[1]})
            no_capture = True
            # AC-PER-43: the field path only; the cue word itself describes the private scene
            privacy = {"no_capture": True, "reason": SLEEP_CUE, "field": cue[1]}
        hold = timedelta(seconds=max(float(self.config.privacy_hold_s or 0.0), 0.0))
        if no_capture and hold:
            until = window.t_end + hold
            self._private_until = until if self._private_until is None else max(self._private_until, until)
        elif not no_capture and self._private_until is not None and window.t_start <= self._private_until:
            # the next overlapping windows may show the sleeper again without saying so
            logger.info("window within the privacy hold of a private window; treated as no_capture",
                        extra={"camera_id": self.camera_id, "window": window.index})
            no_capture = True
            privacy = {"no_capture": True, "reason": PRIVACY_HOLD}
        # AC-PER-41: after every privacy decision, so nothing carries into or out of a no_capture window
        self._module_carry.resolver = self.resolver
        self._module_carry.carry_s = float(self.config.module_carry_s or 0.0)
        carry = self._module_carry.step(window.t_start, window.t_end, scope, no_capture=no_capture,
                                        roaming=self.resolver.is_roaming(camera_location),
                                        scene_change=caption_scene_change(self.resolver, obs.get("location_hint")))
        location = carry.location
        window_location: dict[str, Any] = {"location_id": location, "source": carry.source}
        if carry.ended:
            window_location["carry_ended"] = carry.ended
            logger.info("module carry ended by a scene change",
                        extra={"camera_id": self.camera_id, "window": window.index, "reason": carry.ended})
        if carry.carried:
            window_location |= {"carried_from": _iso(carry.carried_from), "gap_s": carry.gap_s}
            logger.info("window has no module of its own; module carried from an earlier window",
                        extra={"camera_id": self.camera_id, "window": window.index, "location": location,
                               "gap_s": carry.gap_s})
        if scope.hint_conflict and not no_capture:
            logger.warning(
                "location hint names another module than the fixed camera; sightings get low confidence",
                extra={"camera_id": self.camera_id, "camera_location": camera_location, "location": location},
            )
        if omni.truncated:
            logger.warning(
                "omni reply truncated and salvaged; sightings get low confidence and no stock updates",
                extra={"camera_id": self.camera_id, "window": window.index,
                       "objects": len(obs.get("objects") or []), "finish_reason": omni.finish_reason},
            )
        ctx = _WindowContext(self, window, location, no_capture, scope.hint_conflict)
        ctx.truncated = omni.truncated
        ctx.carried = carry.carried
        dets = [list(detections[i]) if i < len(detections) else [] for i in range(len(window.frames))]

        raw_objects = [o for o in obs.get("objects") or [] if isinstance(o, dict)]
        # AC-PER-50: station structure / outside views are not inventory (UNKNOWN labels only); AC-PER-49: body-part
        # labels are person regions, never instances. Both are dropped before consolidation and linking.
        structure = structure_filtered(raw_objects, self.mapper)
        body = [o for o in raw_objects if self._is_body(str(o.get("label") or ""))]
        dropped_ids = {id(o) for o in structure} | {id(o) for o in body}
        objects = [o for o in raw_objects if id(o) not in dropped_ids]
        if structure or body:
            logger.info("omni objects that are not inventory dropped",
                        extra={"window": window.index, "structure": len(structure), "body_parts": len(body)})
        ctx.person_regions = self._person_regions(window, raw_objects, dets)

        observed = self._link(window, objects, dets)
        if query_detect is not None and not no_capture:
            # review round: people omni reports (people[] carries no box) or lists as objects
            people_reported = bool([p for p in obs.get("people") or [] if isinstance(p, dict)]) or any(
                self._is_region_label(str(o.get("label") or "")) for o in raw_objects)
            self._query_unknowns(ctx, observed, dets, query_detect, people_reported)  # AC-PER-54/55
        observed = self._gate_unknowns(ctx, observed)  # AC-PER-59
        observed = self._with_people(window, obs, observed)  # AC-PER-35
        for o in observed:
            o.location = self._object_location(ctx, o)
        fungible = [o for o in observed if self._is_fungible(o)]
        instances = [
            o
            for o in observed
            if not self._is_fungible(o)
            and not (o.is_unknown and no_capture)
            and not (self._is_person(o) and no_capture)  # AC-PER-36: nobody is tracked inside a private zone
        ]

        reviews: list[dict[str, Any]] = []
        self._match_instances(ctx, instances)
        self._check_speech_flags(ctx, instances)
        assignments = [self._assignment(ctx, o) for o in instances]
        self._expected_count_reviews(ctx, instances)
        for o in instances:
            reviews.extend(o.reviews)
        if no_capture:
            reviews = []

        events = self._visual_events(ctx, obs, instances)
        events.extend(self._module_change_events(ctx, instances, assignments, events))  # AC-PER-60
        speech_events, claims = self._speech_events(ctx, transcript, instances)
        events.extend(speech_events)

        # AC-PER-43: a private window carries no text at all (summary, transcript, audio event labels)
        window_doc: dict[str, Any] = {
            "camera_id": self.camera_id,
            "t_start": _iso(window.t_start),
            "t_end": _iso(window.t_end),
            "scene_summary": None if no_capture else str(obs.get("scene_summary") or ""),
            "transcript": None if no_capture else (transcript or ""),
            "audio_events": [] if no_capture else list(obs.get("audio_events") or []),
            "model": model or omni.model or "unknown",
            "private": bool(no_capture),
            "privacy_reason": privacy["reason"] if privacy is not None else None,
        }
        window_doc["location"] = window_location  # AC-PER-41 metadata (inventory-api ignores unknown window fields)
        # AC-PER-49/50 window statistics (inventory-api ignores unknown window fields)
        window_doc["structure_filtered"] = len(structure)
        window_doc["person_crops_dropped"] = ctx.person_crops_dropped
        # AC-PER-54/55 window statistics (inventory-api ignores unknown window fields)
        window_doc["unknown_queries"] = ctx.unknown_queries
        window_doc["unknown_boxes_linked"] = ctx.unknown_boxes_linked
        window_doc["unknown_query_person_rejected"] = ctx.unknown_query_person_rejected
        window_doc["unknown_query_ms"] = ctx.unknown_query_ms
        # AC-PER-59 window statistics (inventory-api ignores unknown window fields)
        window_doc["generic_unknowns_skipped"] = ctx.generic_unknowns_skipped
        window_doc["unknowns_capped"] = ctx.unknowns_capped
        if privacy is not None:
            window_doc["privacy"] = privacy  # AC-PER-37 metadata (inventory-api ignores unknown window fields)
        if not no_capture:
            raw = omni.raw or json.dumps(obs)
            window_doc["raw_uri"] = self.storage.put(
                raw_key(self.camera_id, window.t_end), raw.encode("utf-8"), "application/json"
            )
        stock_updates = self._stock_updates(ctx, fungible, dets)
        if stock_updates and omni.incomplete:
            # AC-PER-26/27: counts from a cut-off or capped object list are lower bounds; stock is overwritten
            # with est_count, so an incomplete window writes no stock update rather than an undercount
            logger.info(
                "incomplete omni observation; stock updates skipped",
                extra={"window": window.index, "truncated": omni.truncated, "dropped_objects": omni.dropped_objects},
            )
            stock_updates = []
        payload: dict[str, Any] = {
            "window": window_doc,
            "assignments": assignments,
            "events": events,
            "reviews": reviews,
            "decisions": ctx.decisions,
            "stock_updates": stock_updates,
        }
        return payload, instances, claims

    def _is_person_label(self, label: str) -> bool:
        item = self.mapper.item(self.mapper.map(label).catalog_id)
        return bool(item and item.get("category") == "person")

    def _is_body(self, label: str) -> bool:
        """AC-PER-49 body-part label. Review round: only an exact/synonym catalog name ("robot hand") is kept as an item,
        and never a face/head label: "head" fuzzy-maps to headset, and its box must stay a person region."""
        if not is_body_label(label):
            return False
        match = self.mapper.map(label)
        if region_kind(label) == "face" or match.catalog_id == UNKNOWN or self._is_person_label(label):
            return True
        return match.method not in ("exact", "synonym")

    def _is_region_label(self, label: str) -> bool:
        return self._is_body(label) or self._is_person_label(label)

    def _person_regions(
        self, window: Window, objects: Sequence[dict[str, Any]], dets: list[list[Detection]]
    ) -> dict[int, list[tuple[tuple[float, float, float, float], str]]]:
        """AC-PER-49: person regions per frame: detector boxes labelled as the person class or a body part, and omni
        object boxes labelled so (on their frame_index; every frame without one). people[] carries no box."""
        n = len(window.frames)
        regions: dict[int, list[tuple[tuple[float, float, float, float], str]]] = {}
        for f, frame_dets in enumerate(dets):
            for det in frame_dets:
                if self._is_region_label(det.label):
                    regions.setdefault(f, []).append((tuple(float(v) for v in det.bbox), region_kind(det.label)))
        for o in objects:
            label = str(o.get("label") or "")
            box = o.get("bbox")
            if not self._is_region_label(label) or not isinstance(box, list) or len(box) != 4:
                continue
            try:
                b = tuple(float(v) for v in box)
            except (TypeError, ValueError):
                continue
            fi = o.get("frame_index")
            frames = [fi] if isinstance(fi, int) and not isinstance(fi, bool) and 0 <= fi < n else range(n)
            for f in frames:
                regions.setdefault(f, []).append((b, region_kind(label)))
        return regions

    def _det_allowed(self, match: LabelMatch, det: Detection) -> bool:
        """AC-PER-49: a body-part detection links to nothing; a person detection only to a person instance."""
        if self._is_body(det.label):
            return False
        if self._is_person_label(det.label):
            item = self.mapper.item(match.catalog_id)
            return bool(item and item.get("category") == "person")
        return True

    def _overlaps_person(self, ctx: _WindowContext, o: _Observed) -> bool:
        regions = ctx.regions(o.frame_idx)
        if o.bbox is None or not regions:
            return False
        return overlaps_person(o.bbox, [r[0] for r in regions], [r[1] for r in regions])

    def _same_area(self, a: str | None, b: str | None) -> bool:
        """AC-PER-51: one place for event purposes: equal, one inside the other, or the same module."""
        if a is None or b is None:
            return False
        if a == b or self.resolver.within(a, b) or self.resolver.within(b, a):
            return True
        module = self.resolver.module_of(a)
        return module is not None and module == self.resolver.module_of(b)

    def _object_location(self, ctx: _WindowContext, o: _Observed) -> str | None:
        """Camera-configured zone (config.camera_zones) containing the centre of the object's box, when it is a
        known, capturable location inside the window location; else the window location. Smallest zone wins."""
        zones = self.config.camera_zones.get(self.camera_id) or {}
        if not zones or o.bbox is None or ctx.location is None:
            return ctx.location
        cx, cy = (o.bbox[0] + o.bbox[2]) / 2.0, (o.bbox[1] + o.bbox[3]) / 2.0
        best: tuple[float, str] | None = None
        for zone_id, (x1, y1, x2, y2) in zones.items():
            if not (x1 <= cx <= x2 and y1 <= cy <= y2):
                continue
            if not self.resolver.exists(zone_id) or self.resolver.is_no_capture(zone_id):
                continue
            if not self.resolver.within(zone_id, ctx.location):
                continue
            area = (x2 - x1) * (y2 - y1)
            if best is None or area < best[0]:
                best = (area, zone_id)
        return best[1] if best else ctx.location

    def _is_fungible(self, o: _Observed) -> bool:
        item = self.mapper.item(o.catalog_id)
        return bool(item and item.get("is_fungible"))

    def _is_person(self, o: _Observed) -> bool:
        item = self.mapper.item(o.catalog_id)
        return bool(item and item.get("category") == "person")

    def _person_class(self) -> dict[str, Any] | None:
        return next((c for c in self.catalog if c.get("category") == "person" and c.get("catalog_id")), None)

    def _with_people(self, window: Window, obs: dict[str, Any], observed: list[_Observed]) -> list[_Observed]:
        """AC-PER-35: every people[] entry (first per ref) is a person sighting. Entries are paired with person
        objects by appearance similarity (>= person_match_floor, best first; a single leftover of each is paired
        too; several leftovers are paired in order without a box). Unpaired entries become sightings of their own."""
        for o in observed:
            if self._is_person(o):  # review round: object text is model output too
                o.obj = {**o.obj, "description": sanitise_text(o.obj.get("description"))}
        people = [p for p in obs.get("people") or [] if isinstance(p, dict)]
        if not people:
            return observed
        pclass = self._person_class()
        if pclass is None:
            logger.info("no person class in the catalog; people[] ignored", extra={"window": window.index})
            return observed
        entries: list[tuple[str, str]] = []
        refs: set[str] = set()
        for p in people:
            ref = str(p.get("ref") or "")
            if ref and ref in refs:
                continue
            refs.add(ref)
            entries.append((ref, sanitise_text(p.get("appearance"))))
        persons = [o for o in observed if self._is_person(o)]
        floor = self.config.person_match_floor
        scored = sorted(
            (
                (-appearance_similarity(o.obj.get("description"), text), oi, ei)
                for oi, o in enumerate(persons)
                for ei, (_, text) in enumerate(entries)
            )
        )
        pairs: dict[int, int] = {}  # person object index -> entry index
        used: set[int] = set()
        for neg_sim, oi, ei in scored:
            if -neg_sim < floor or oi in pairs or ei in used:
                continue
            pairs[oi] = ei
            used.add(ei)
        left_objects = [oi for oi in range(len(persons)) if oi not in pairs]
        left_entries = [ei for ei in range(len(entries)) if ei not in used]
        boxless: set[int] = set()

        def clash(oi: int, ei: int) -> bool:
            return appearance_conflict(persons[oi].obj.get("description"), entries[ei][1])

        if len(left_objects) == 1 and len(left_entries) == 1:
            if not clash(left_objects[0], left_entries[0]):
                pairs[left_objects[0]] = left_entries[0]
                used.add(left_entries[0])
        elif left_objects and left_entries:
            for oi in left_objects:
                ei = next((e for e in left_entries if e not in used and not clash(oi, e)), None)
                if ei is None:
                    continue
                pairs[oi] = ei
                used.add(ei)
                boxless.add(oi)
            logger.info("person objects and people[] paired in order without a box",
                        extra={"window": window.index, "pairs": len(boxless)})
        for oi, ei in pairs.items():
            o = persons[oi]
            text = entries[ei][1]
            if text:
                o.obj = {**o.obj, "description": text}
            if oi in boxless:
                o.bbox, o.detection = None, None
        match = LabelMatch(str(pclass["catalog_id"]), 1.0, "people", pclass.get("class_name"))
        base = max((max(o.members or [o.index]) for o in observed), default=-1) + 1
        base = max(base, len(obs.get("objects") or []))
        last = max(len(window.frames) - 1, 0)
        out = list(observed)
        for k, ei in enumerate(ei for ei in range(len(entries)) if ei not in used):
            index = base + k
            out.append(
                _Observed(
                    index=index,
                    obj={"label": "person", "description": entries[ei][1]},
                    match=match,
                    frame_idx=last,
                    bbox=None,
                    detection=None,
                    held=False,
                    ref=f"p{index}",
                    members=[index],
                )
            )
        return out

    def _link(self, window: Window, objects: Sequence[dict[str, Any]], dets: list[list[Detection]]) -> list[_Observed]:
        """Consolidate the omni objects (AC-PER-19), then link each instance to a detector box: IoU links for all
        instances first, then label links for the rest; each box is used once."""
        n_frames = len(window.frames)
        entries = []
        for index, obj in enumerate(objects):
            fi = obj.get("frame_index")
            fi = fi if isinstance(fi, int) and not isinstance(fi, bool) and 0 <= fi < n_frames else None
            box = obj.get("bbox")
            box = tuple(float(v) for v in box) if isinstance(box, list) and len(box) == 4 else None
            entries.append(_Entry(index, obj, self.mapper.map(str(obj.get("label") or "")), fi, box))
        groups = self._consolidate(entries, dets)
        if len(groups) < len(entries):
            logger.info(
                "omni objects consolidated within the window",
                extra={"window": window.index, "objects": len(entries), "instances": len(groups)},
            )
        used: set[tuple[int, int]] = set()
        links: list[_Link | None] = [None] * len(groups)
        # pass 1: detector boxes that agree with omni's box; same-class boxes for every instance first, then (AC-PER-58
        # review round) another class's box for unknowns, so an unknown listed first never takes a catalog box
        for cross in (False, True):
            for gi, group in enumerate(groups):
                if links[gi] is not None:
                    continue
                best = None
                for e in group:
                    hit = self._iou_link(e, dets, used, n_frames, cross=cross)
                    if hit is not None and (best is None or hit.rank > best.rank):
                        best = hit
                if best is not None:
                    used.add(best.det)
                    links[gi] = best
        for gi, group in enumerate(groups):  # pass 2: same label in the requested / latest frame
            if links[gi] is not None:
                continue
            best = None
            for e in group:
                frames = [e.frame_index] if e.frame_index is not None else list(range(n_frames - 1, -1, -1))
                det = self._link_by_label(e.match, e.obj, frames, dets, used)
                if det is not None:
                    hit = _Link(e, det[0], det, dets[det[0]][det[1]])
                else:
                    hit = _Link(e, e.frame_index if e.frame_index is not None else max(n_frames - 1, 0))
                if best is None or hit.rank > best.rank:
                    best = hit
            if best is not None and best.det is not None:
                used.add(best.det)
            links[gi] = best
        out: list[_Observed] = []
        for group, link in zip(groups, links, strict=True):
            assert link is not None
            rep, detection = link.entry, link.detection
            obj = dict(rep.obj)
            holder = next((e.obj.get("held_by") for e in group if e.obj.get("held_by")), None)
            if holder and not obj.get("held_by"):
                obj["held_by"] = holder
            out.append(
                _Observed(
                    index=rep.index,
                    obj=obj,
                    match=rep.match,
                    frame_idx=link.frame_idx,
                    bbox=tuple(float(v) for v in detection.bbox) if detection else rep.box,
                    detection=detection,
                    held=holder is not None,
                    ref=f"o{rep.index}",
                    members=sorted(e.index for e in group),
                )
            )
        return out

    def _query_text(self, label: str) -> str | None:
        """AC-PER-54/55: the detector query for an unknown's label, or None (empty, structure, body, person class or any
        person word: a query naming a person would return the person's box)."""
        text = noun_phrase(label)
        if not text or is_structure_label(label) or mentions_person(label) or self._is_region_label(label):
            return None
        return text

    def _query_box_overlaps_person(self, ctx: _WindowContext, frame_idx: int, box: Sequence[float]) -> bool:
        regions = ctx.regions(frame_idx)
        return bool(regions) and overlaps_person(box, [r[0] for r in regions], [r[1] for r in regions])

    def _query_unknowns(
        self,
        ctx: _WindowContext,
        observed: list[_Observed],
        dets: list[list[Detection]],
        query_detect: QueryDetect,
        people_reported: bool = False,
    ) -> None:
        """AC-PER-54/55: unknown instances without a detector box get one from an extra detector call that queries
        their own label on their frame: at most ``unknown_queries_max`` distinct texts (omni confidence order), one
        call per frame, only the new texts. The best unclaimed box of the instance's text is linked when its score is
        >= ``unknown_query_min_score`` and it overlaps no person region; otherwise nothing is linked. Boxes of the main
        detector call (any label) and boxes linked to other instances are never taken. When omni reports people but
        the detector boxed nobody in any frame of the window, no query box is linked (fail closed: the crop guard
        would have no region to check against)."""
        budget = int(self.config.unknown_queries_max or 0)
        n_frames = len(ctx.window.frames)
        if budget <= 0 or n_frames == 0:
            return
        pending = sorted(
            (o for o in observed if o.is_unknown and o.detection is None and 0 <= o.frame_idx < n_frames),
            key=lambda o: (-_clamp01(o.obj.get("confidence"), 0.5), o.index),
        )
        texts: list[str] = []
        chosen: list[tuple[_Observed, str]] = []
        for o in pending:
            text = self._query_text(str(o.obj.get("label") or ""))
            if text is None:
                continue
            if text not in texts:
                if len(texts) >= budget:
                    continue
                texts.append(text)
            chosen.append((o, text))
        if not chosen:
            return
        by_frame: dict[int, list[str]] = {}
        for o, text in chosen:
            frame_texts = by_frame.setdefault(o.frame_idx, [])
            if text not in frame_texts:
                frame_texts.append(text)
        started = time.monotonic()
        found: dict[int, list[Detection]] = {}
        ran: set[str] = set()
        for f, frame_texts in sorted(by_frame.items()):
            try:
                found[f] = [d for d in query_detect(f, list(frame_texts)) or [] if d.label in frame_texts]
                ran.update(frame_texts)
            except Exception as exc:  # noqa: BLE001 - the extra boxes are optional; the window is still written
                logger.warning("unknown-object query failed", extra={"window": ctx.window.index, "frame": f,
                                                                      "error": str(exc)})
                found[f] = []
        ctx.unknown_query_ms = int((time.monotonic() - started) * 1000)
        ctx.unknown_queries = len(ran)
        # boxes that belong to other objects: every main-call detection of the frame, linked or not (review round),
        # and boxes linked to other instances
        claimed: dict[int, list[tuple[float, float, float, float]]] = {}
        for f, frame_dets in enumerate(dets):
            claimed.setdefault(f, []).extend(tuple(float(v) for v in d.bbox) for d in frame_dets)
        for o in observed:
            if o.detection is not None and o.bbox is not None:
                claimed.setdefault(o.frame_idx, []).append(o.bbox)
        nobody_boxed = people_reported and not any(ctx.person_regions.values())
        min_score = float(self.config.unknown_query_min_score)
        for o, text in chosen:
            f = o.frame_idx
            taken = claimed.get(f, [])
            ranked = sorted((d for d in found.get(f, []) if d.label == text), key=lambda d: -d.score)
            best = next((d for d in ranked if not any(iou(d.bbox, b) >= self.config.link_iou for b in taken)), None)
            if best is None or best.score < min_score:
                continue
            if nobody_boxed or self._query_box_overlaps_person(ctx, f, best.bbox):
                ctx.unknown_query_person_rejected += 1  # AC-PER-55: never linked, no lower box instead
                logger.info("unknown-object query box overlaps a person region; not linked",
                            extra={"window": ctx.window.index, "frame": f})
                continue
            o.bbox = ordered(best.bbox)
            o.detection = best
            claimed.setdefault(f, []).append(o.bbox)
            ctx.unknown_boxes_linked += 1
        logger.info(
            "unknown-object queries",
            extra={"window": ctx.window.index, "queries": len(texts), "frames": sorted(by_frame), "calls": len(by_frame),
                   "linked": ctx.unknown_boxes_linked, "person_rejected": ctx.unknown_query_person_rejected,
                   "ms": ctx.unknown_query_ms},
        )

    def _query_linked(self, o: _Observed) -> bool:
        """AC-PER-59: the instance's detector box came from the AC-PER-54 query of its own label."""
        text = noun_phrase(str(o.obj.get("label") or ""))
        return o.detection is not None and bool(text) and normalise_label(o.detection.label) == text

    def _gate_unknowns(self, ctx: _WindowContext, observed: list[_Observed]) -> list[_Observed]:
        """AC-PER-59 (UI5-02): a generic colour+noun unknown ("grey object") is kept only with a box from the query of
        its own label (AC-PER-54); then at most ``unknowns_per_frame_max`` unknowns per frame and
        ``unknowns_per_window_max`` per window, detector-linked first, then by omni confidence. Catalog instances are
        never affected. Dropped instances are counted (window statistics) and logged without labels."""
        unknowns: list[_Observed] = []
        for o in observed:
            if not o.is_unknown:
                continue
            if is_generic_label(str(o.obj.get("label") or ""), str(o.obj.get("description") or "")) and not (
                self._query_linked(o)
            ):
                ctx.generic_unknowns_skipped += 1
            else:
                unknowns.append(o)
        per_frame = int(self.config.unknowns_per_frame_max)
        per_window = int(self.config.unknowns_per_window_max)
        frames: Counter[int] = Counter()
        kept_unknown: set[int] = set()
        for o in sorted(unknowns, key=lambda u: (u.detection is None, -_clamp01(u.obj.get("confidence"), 0.5),
                                                  u.index)):
            if (per_window > 0 and len(kept_unknown) >= per_window) or (
                per_frame > 0 and frames[o.frame_idx] >= per_frame
            ):
                ctx.unknowns_capped += 1
                continue
            frames[o.frame_idx] += 1
            kept_unknown.add(id(o))
        if ctx.generic_unknowns_skipped or ctx.unknowns_capped:
            logger.info("unknown objects held back", extra={
                "window": ctx.window.index, "generic_skipped": ctx.generic_unknowns_skipped,
                "capped": ctx.unknowns_capped})
        return [o for o in observed if not o.is_unknown or id(o) in kept_unknown]

    def _consolidation_key(self, e: _Entry) -> tuple[str, str] | None:
        """Instances of the same key may be consolidated; None = never (fungible, or an empty UNKNOWN label)."""
        if e.match.catalog_id == UNKNOWN:
            label = label_key(str(e.obj.get("label") or ""))  # AC-PER-31
            return ("unknown", label) if label else None
        item = self.mapper.item(e.match.catalog_id)
        if item and item.get("is_fungible"):
            return None  # AC-PER-9 counts per frame
        return ("class", e.match.catalog_id)

    def _same_instance(self, a: _Entry, b: _Entry, dets: list[list[Detection]]) -> bool:
        same_frame = a.frame_index is not None and a.frame_index == b.frame_index
        if a.box is None or b.box is None:
            return not same_frame
        if iou(a.box, b.box) < self.config.consolidate_iou:
            return False
        if same_frame:
            # Listed twice in one frame with overlapping boxes: a repeat, unless the detector sees two distinct
            # objects of the class there (stacked laptops).
            da, db = self._frame_detection(a, dets), self._frame_detection(b, dets)
            if da is not None and db is not None and da != db:
                return False
        return True

    def _frame_detection(self, e: _Entry, dets: list[list[Detection]]) -> int | None:
        """Index of the same-class detection in ``e``'s frame that best overlaps its box (IoU >= link_iou)."""
        if e.frame_index is None or e.box is None or e.frame_index >= len(dets):
            return None
        best, found = 0.0, None
        for d_i, det in enumerate(dets[e.frame_index]):
            if not self._det_allowed(e.match, det):
                continue
            det_match = self.mapper.map(det.label)
            if e.match.catalog_id != UNKNOWN and det_match.catalog_id != e.match.catalog_id:
                continue
            overlap = iou(e.box, det.bbox)
            if overlap >= self.config.link_iou and overlap > best:
                best, found = overlap, d_i
        return found

    def _consolidate(self, entries: list[_Entry], dets: list[list[Detection]]) -> list[list[_Entry]]:
        """Leader clustering (AC-PER-19): visit by omni confidence (then window order); join the first cluster whose
        leader has the same key and is the same instance. Not transitive. Clusters in window order."""
        clusters: list[list[_Entry]] = []
        for e in sorted(entries, key=lambda x: (-x.confidence, x.index)):
            key = self._consolidation_key(e)
            target = None
            if key is not None:
                target = next(
                    (
                        c
                        for c in clusters
                        if self._consolidation_key(c[0]) == key and self._same_instance(c[0], e, dets)
                    ),
                    None,
                )
            if target is None:
                clusters.append([e])
            else:
                target.append(e)
        return sorted(clusters, key=lambda c: min(x.index for x in c))

    def _iou_link(
        self, e: _Entry, dets: list[list[Detection]], used: set[tuple[int, int]], n_frames: int, *, cross: bool = False
    ):
        """AC-PER-58: ``cross`` False = boxes of the entry's own class only; True = another class's box, for an
        UNKNOWN entry only, at IoU >= ``link_iou_cross_class``."""
        if e.box is None or (cross and e.match.catalog_id != UNKNOWN):
            return None
        frames = [e.frame_index] if e.frame_index is not None else list(range(n_frames - 1, -1, -1))
        best: tuple[bool, float] | None = None
        linked = None
        for f in frames:
            for d_i, det in enumerate(dets[f]):
                if (f, d_i) in used or not self._det_allowed(e.match, det):
                    continue
                same = self._same_class(e, det)
                if same == cross:
                    continue  # AC-PER-58: a catalog instance never takes another class's box
                need = self.config.link_iou if same else max(self.config.link_iou, self.config.link_iou_cross_class)
                overlap = iou(e.box, det.bbox)
                if overlap >= need and (best is None or (same, overlap) > best):
                    best, linked = (same, overlap), (f, d_i)
        if linked is None:
            return None
        return _Link(e, linked[0], linked, dets[linked[0]][linked[1]], agrees=True)

    def _same_class(self, e: _Entry, det: Detection) -> bool:
        """AC-PER-58: the detection is of the entry's class (an UNKNOWN entry: the detection's label is its label)."""
        if e.match.catalog_id == UNKNOWN:
            return normalise_label(det.label) == normalise_label(str(e.obj.get("label") or ""))
        return self.mapper.map(det.label).catalog_id == e.match.catalog_id

    def _link_by_label(
        self,
        match: LabelMatch,
        obj: dict[str, Any],
        frames: list[int],
        dets: list[list[Detection]],
        used: set[tuple[int, int]],
    ) -> tuple[int, int] | None:
        label = normalise_label(str(obj.get("label") or ""))
        best: tuple[float, tuple[int, int]] | None = None
        for f in frames:
            for d_i, det in enumerate(dets[f]):
                if (f, d_i) in used or not self._det_allowed(match, det):
                    continue
                det_match = self.mapper.map(det.label)
                same = (
                    det_match.catalog_id == match.catalog_id
                    if match.catalog_id != UNKNOWN
                    else normalise_label(det.label) == label
                )
                if same and (best is None or det.score > best[0]):
                    best = (det.score, (f, d_i))
            if best is not None:
                break  # prefer the requested / latest frame
        return best[1] if best else None

    # ------------------------------------------------------------------ matching
    def _features(self, ctx: _WindowContext, o: _Observed, cand: dict[str, Any]) -> CandidateFeatures | None:
        """Hard gates (None = gated) and score features for one candidate."""
        cand_catalog = cand.get("catalog_id") or None
        if o.is_unknown:
            # An UNKNOWN label is not a class: it may be an unclassified entity (class match) or an entity NASA has
            # classified since (review resolution) that the model still describes in free text (no class match).
            class_match = cand_catalog is None
        elif cand_catalog != o.catalog_id:
            return None  # class mismatch
        else:
            class_match = True
        seen_at = ctx.frame_ts(o.frame_idx)
        last_seen = _parse_ts(cand.get("last_seen_at"))
        recency = abs((seen_at - last_seen).total_seconds()) if last_seen else None
        last_loc = cand.get("last_location")
        if not o.held and recency is not None and recency < self.config.min_transit_s:
            m_now, m_then = self.resolver.module_of(o.location), self.resolver.module_of(last_loc)
            if m_now and m_then and m_now != m_then:
                return None  # impossible transit for an object nobody is carrying
        sim = cand.get("similarity")
        return CandidateFeatures(
            similarity=float(sim) if sim is not None else 0.0,
            class_match=class_match,
            zone_match=self.resolver.same_module(last_loc, o.location),
            recency_s=recency,
        )

    def _exemplar_ok(self, o: _Observed, image: np.ndarray) -> bool:
        """AC-PER-22: only a detector-localised crop of at least ``min_crop_px`` (short side) is an exemplar."""
        return o.detection is not None and o.bbox is not None and min(image.shape[:2]) >= self.config.min_crop_px

    def _match_instances(self, ctx: _WindowContext, instances: list[_Observed]) -> None:
        embedded: list[_Observed] = []
        gated: list[_Observed] = []
        persons: list[_Observed] = []
        for o in instances:
            if self._is_person(o):
                persons.append(o)  # AC-PER-36: never embedded (a person crop is face-bearing, biometric-like)
                continue
            image = self._crop(ctx, o)
            if not self._exemplar_ok(o, image):
                gated.append(o)
                continue
            if self._overlaps_person(ctx, o):
                # AC-PER-49: a crop that overlaps a person is never embedded, stored or uploaded
                o.person_overlap = True
                ctx.person_crops_dropped += 1
                logger.info("object crop overlaps a person region; crop dropped",
                            extra={"window": ctx.window.index, "frame": o.frame_idx})
                gated.append(o)
                continue
            embedded.append(o)
            o.embedding = np.asarray(self.embedder.embed(image), dtype=np.float32).reshape(-1)
            candidates = (
                self.api.candidates(
                    [float(v) for v in o.embedding],
                    catalog_id=o.catalog_id,
                    location_id=o.location,
                    k=max(self.config.candidates_k, self.config.unknown_candidates_k)
                    if o.is_unknown
                    else self.config.candidates_k,
                )
                or []
            )
            o.candidate_ids = {str(c.get("entity_id")) for c in candidates}
            scored = []
            for cand in candidates:
                features = self._features(ctx, o, cand)
                if features is not None:
                    scored.append(_Scored(cand, _clamp01(self.scorer(features), 0.0)))
            o.scored = _rank(scored, o.location)
            if self._is_generic(o):  # AC-PER-60: another module needs the auto band for a generic class
                o.held_cross_module = [s for s in o.scored if s.p < self.config.assign_p
                                       and self._cross_module(s.candidate.get("last_location"), o.location)]
                if o.held_cross_module:
                    held = {s.entity_id for s in o.held_cross_module}
                    o.scored = [s for s in o.scored if s.entity_id not in held]
                    logger.info("generic-class candidates in another module held back (below the auto band)",
                                extra={"window": ctx.window.index, "class": o.catalog_id, "held": len(held)})
        # One entity per window: the object with the strongest match chooses first (ties: window order); instances
        # without an exemplar (AC-PER-22) come last and match by class and place only.
        taken: set[str] = set()
        for o in sorted(embedded, key=lambda x: (-(x.scored[0].p if x.scored else -1.0), x.index)):
            self._decide(ctx, o, [s for s in o.scored if s.entity_id not in taken])
            if o.entity_id:
                taken.add(o.entity_id)
        # An embedded instance that matched nothing may be an entity created from exemplar-less sightings (never a
        # candidate, it has no exemplar): adopt it by class and place, never an entity already judged different by
        # appearance. Its exemplar then becomes that entity's first one.
        for o in embedded:
            if o.entity_id is None and not o.reviews:
                self._match_without_exemplar(ctx, o, taken | o.candidate_ids)
                if o.entity_id:
                    taken.add(o.entity_id)
        for o in embedded:  # AC-PER-60 (review round: after adoption, only ask-band look-alikes)
            held = [s for s in o.held_cross_module if s.p >= self.config.ask_p and s.entity_id not in taken]
            if o.entity_id is None and not o.reviews and held:
                # a new entity; the look-alike in another module is for an operator to judge
                o.reviews.append({
                    "kind": "possible_duplicate",
                    "candidate_ids": [s.entity_id for s in held[:MAX_DECISION_OPTIONS]],
                    "probabilities": {},
                    "description": self._review_description(
                        o, "possible duplicate of the same class last seen in another module"),
                })
        for o in gated:
            self._match_without_exemplar(ctx, o, taken)
            if o.entity_id:
                taken.add(o.entity_id)
        self._match_people(ctx, persons, taken)

    def _match_people(self, ctx: _WindowContext, persons: list[_Observed], taken: set[str]) -> None:
        """AC-PER-35/45/47: person sightings match live person-class entities by appearance text, assigned globally best
        first (similarity, closeness, same place before cross-module, recent before stale, own zone, most recent).
        Candidates: the same module (AC-PER-29 listing; outside modules the exact location, recent only) and, for an
        observation with appearance descriptors, any module within ``person_cross_module_s`` (AC-PER-45). A sighting
        without descriptors attaches to the most recent same-place recent candidate. Sightings left over when the
        live count has reached the class's expected_count may attach to any live person entity (AC-PER-47). Never two
        sightings of a window on one entity, never a colour clash, always >= ``person_match_floor``."""
        located = [o for o in persons if o.location is not None and self._place_key(o) is not None]
        ranked = [(rank, o, item) for o in located for rank, item in self._person_candidates(ctx, o, taken)]
        had_candidate = {o.index for _, o, _ in ranked}
        self._assign_people(ranked, taken)
        # review round: only sightings that found no candidate at all (not those that lost one to another person of
        # this window: that person is a different one, a stale look-alike elsewhere is no better)
        left = [o for o in located if o.entity_id is None and o.index not in had_candidate]
        if left and self.config.person_soft_cap and self._expected_count_reached(ctx, left[0]):
            ranked = [(rank, o, item) for o in left for rank, item in self._person_candidates(ctx, o, taken, cap=True)]
            if ranked:
                logger.info("person soft cap: attaching instead of new tracks", extra={"window": ctx.window.index})
            self._assign_people(ranked, taken)

    def _person_candidates(
        self, ctx: _WindowContext, o: _Observed, taken: set[str], *, cap: bool = False
    ) -> list[tuple[tuple, dict[str, Any]]]:
        """(rank, item) of the eligible person entities for ``o``; ``cap``: AC-PER-47, any place and any gap."""
        cfg = self.config
        key = self._place_key(o)
        module = self.resolver.module_of(o.location)
        seen_at = ctx.frame_ts(o.frame_idx)
        desc = str(o.obj.get("description") or "")
        featureless = not appearance_descriptors(desc)
        if cap and featureless:
            return []
        pool: dict[str, tuple[dict[str, Any], bool]] = {}  # entity id -> (item, same place)
        for item in self._place_items(ctx, key, module or o.location) or []:
            if self._same_place(item.get("last_location"), o.location):
                pool[str(item.get("entity_id") or "")] = (item, True)
        if not featureless and (cap or cfg.person_cross_module_s > 0):
            for eid, item in self._class_items(ctx, o.catalog_id).items():
                pool.setdefault(eid, (item, False))
        out: list[tuple[tuple, dict[str, Any]]] = []
        for eid, (item, same) in pool.items():
            last_seen = _parse_ts(item.get("last_seen_at"))
            if not eid or eid in taken or item.get("status") in ("merged", "dismissed") or last_seen is None:
                continue
            if item.get("catalog_id") not in (None, o.catalog_id):
                continue
            gap = abs((seen_at - last_seen).total_seconds())
            recent = gap <= cfg.no_exemplar_match_s
            if not cap:
                if same and not recent and (module is None or featureless):
                    continue
                if not same and gap > cfg.person_cross_module_s:
                    continue
            other = str(item.get("description") or "")
            sim = close = 0.0
            if not featureless:
                if appearance_conflict(desc, other) or any(
                    appearance_conflict(desc, seen) for seen in self._person_recent.get(eid, ())
                ):
                    continue  # a colour clash with the track (first or latest sightings) is a different person
                sim = appearance_similarity(desc, other)
                if sim < cfg.person_match_floor:
                    continue
                close = appearance_closeness(desc, other)
                # review round: another module or the cap is weak spatial evidence; the appearance must carry it
                # two-sided (Dice >= floor, so a short first description cannot absorb richer outfits) and with a
                # specific shared trait (a compatibly coloured garment, a distinctive accessory or insignia)
                if (cap or not same) and (close < cfg.person_match_floor or not appearance_evidence(desc, other)):
                    continue
            rank = (featureless, -sim, -close, not same, not recent, item.get("last_location") != o.location,
                    -last_seen.timestamp(), o.index, eid)
            out.append((rank, item))
        return out

    def _assign_people(self, ranked: list[tuple[tuple, _Observed, dict[str, Any]]], taken: set[str]) -> set[int]:
        done: set[int] = set()
        for _, o, item in sorted(ranked, key=lambda r: r[0]):
            eid = str(item["entity_id"])
            if o.index in done or eid in taken:
                continue
            self._attach_item(o, item)
            done.add(o.index)
            taken.add(eid)
        return done

    def _match_without_exemplar(self, ctx: _WindowContext, o: _Observed, taken: set[str]) -> None:
        """AC-PER-22/29 class-and-place match. Candidates: live entities of the same class (UNKNOWN: unclassified, same
        label key) last seen in the observation's module or one of its zones (outside modules: the same location),
        not ``taken``. Any seen within ``no_exemplar_match_s`` -> one last seen in the observation's own zone, else
        the most recent. Otherwise (a revisit, any gap,
        modules only): one candidate -> attach; several -> the decision model, with a same-zone / most-recent
        fallback + low_confidence review. No candidate -> stays new."""
        key = self._place_key(o)
        if key is None or o.location is None:
            return
        module = self.resolver.module_of(o.location)
        items = self._place_items(ctx, key, module or o.location)
        if not items:
            return
        seen_at = ctx.frame_ts(o.frame_idx)
        candidates: list[tuple[datetime, dict[str, Any]]] = []
        for item in items:
            eid = str(item.get("entity_id") or "")
            last_seen = _parse_ts(item.get("last_seen_at"))
            if not eid or eid in taken or item.get("status") in ("merged", "dismissed") or last_seen is None:
                continue
            if self._same_place(item.get("last_location"), o.location):
                candidates.append((last_seen, item))
        if not candidates:
            return
        horizon = self.config.no_exemplar_match_s
        recent = [c for c in candidates if abs((seen_at - c[0]).total_seconds()) <= horizon]
        if recent:  # review round: a candidate last seen in the observation's own zone first, then the most recent
            self._attach_item(o, min(recent, key=lambda c: (c[1].get("last_location") != o.location, -c[0].timestamp()))[1])
            return
        if module is None:
            return  # a segment-level place is too coarse to identify an object after an arbitrary gap
        if len(candidates) == 1:
            self._attach_item(o, candidates[0][1])
            return
        self._decide_revisit(ctx, o, candidates)

    def _place_key(self, o: _Observed) -> tuple[str, str] | None:
        if o.is_unknown:
            key = label_key(str(o.obj.get("label") or ""))
            return ("unknown", key) if key else None
        return ("class", o.catalog_id) if o.catalog_id else None

    def _place_items(
        self, ctx: _WindowContext, key: tuple[str, str], place: str
    ) -> list[dict[str, Any]] | None:
        """Live entities of ``key`` last seen in ``place`` or below it; one GET /items per (key, place) per window.
        None when inventory-api failed (the sighting then stays new)."""
        cache_key = (key, place)
        if cache_key in ctx.place_items:
            return ctx.place_items[cache_key]
        kind, value = key
        try:
            if kind == "unknown":
                rows = self.api.list_items(location=place, sort="-last_seen_at", limit=UNKNOWN_PLACE_ITEMS_LIMIT) or []
                items = [r for r in rows if not r.get("catalog_id") and label_key(str(r.get("label") or "")) == value]
            else:
                rows = self.api.list_items(
                    catalog_id=value, location=place, sort="-last_seen_at", limit=PLACE_ITEMS_LIMIT
                ) or []
                items = [r for r in rows if r.get("catalog_id") == value]
        except Exception as exc:  # noqa: BLE001 - inventory-api is a remote dependency; fall back to a new entity
            logger.warning("listing entities for a sighting without exemplar failed", extra={"error": str(exc)})
            items = None
        ctx.place_items[cache_key] = items
        return items

    def _attach_item(self, o: _Observed, item: dict[str, Any]) -> None:
        o.entity_id = str(item["entity_id"])
        o.previous_location = item.get("last_location")

    def _live_count(self, ctx: _WindowContext, catalog_id: str) -> int:
        if catalog_id not in ctx.live_counts:
            ctx.live_counts[catalog_id] = int(self.api.count_live_entities(catalog_id))
        return ctx.live_counts[catalog_id]

    def _expected_count_reached(self, ctx: _WindowContext, o: _Observed) -> bool:
        item = self.mapper.item(o.catalog_id)
        expected = item.get("expected_count") if item else None
        if expected is None or item.get("is_fungible"):
            return False
        try:
            return self._live_count(ctx, o.catalog_id) >= int(expected)
        except Exception as exc:  # noqa: BLE001
            logger.warning("counting live entities failed", extra={"catalog_id": o.catalog_id, "error": str(exc)})
            return False

    def _decide_revisit(
        self, ctx: _WindowContext, o: _Observed, candidates: list[tuple[datetime, dict[str, Any]]]
    ) -> None:
        """AC-PER-29: several stale candidates for a sighting without exemplar. Same-zone candidates first, then the
        most recent; the decision model chooses among the first MAX_DECISION_OPTIONS + NEW."""
        cfg = self.config
        seen_at = ctx.frame_ts(o.frame_idx)
        ranked = sorted(
            candidates,
            key=lambda c: (c[1].get("last_location") != o.location, -c[0].timestamp(), str(c[1]["entity_id"])),
        )[:MAX_DECISION_OPTIONS]
        ids = [str(item["entity_id"]) for _, item in ranked]
        options = {
            str(item["entity_id"]): self._describe_entity(item, last_seen, seen_at) for last_seen, item in ranked
        }
        options[NEW] = f"a different {o.match.class_name or o.obj.get('label') or 'object'} not listed here"
        observed_at = _iso(seen_at)
        subject = {
            "camera_id": self.camera_id,
            "observed_at": observed_at,
            "label": self._subject_label(ctx, o),
            "catalog_id": o.catalog_id,
            "location_id": o.location,
            "basis": "class_and_place_revisit",
            "candidates": [
                {"entity_id": str(item["entity_id"]), "last_location": item.get("last_location"),
                 "last_seen_at": _iso(last_seen)}
                for last_seen, item in ranked
            ],
        }
        state = {
            "observation": self._observation_state(o, observed_at),
            "candidates": {
                str(item["entity_id"]): {
                    "label": item.get("label"),
                    "description": str(item.get("description") or ""),
                    "last_location": item.get("last_location"),
                    "last_seen_at": _iso(last_seen),
                    "seen_ago_s": round((seen_at - last_seen).total_seconds(), 1),
                }
                for last_seen, item in ranked
            },
        }
        result = self._choose(
            SAME_OR_NEW,
            options,
            state,
            "No appearance vector is available: decide from the descriptions and places whether the observed object "
            "is one of the listed entities (seen here before), or NEW.",
        )
        ctx.log_decision(SAME_OR_NEW, subject, result)
        choice_p = float(result.probabilities.get(result.choice, 0.0)) if result.probabilities else 0.0
        confident = not result.needs_review and choice_p >= cfg.decision_accept
        if confident and result.choice in ids:
            self._attach_item(o, ranked[ids.index(result.choice)][1])
            return
        if confident and result.choice == NEW and not self._expected_count_reached(ctx, o):
            return  # confidently a different object
        self._attach_item(o, ranked[0][1])  # fallback: same zone, else the most recently seen
        o.reviews.append(
            {
                "kind": LOW_CONFIDENCE,
                "candidate_ids": ids,
                "probabilities": dict(result.probabilities),
                "description": self._review_description(
                    o, "attached by class and place after a revisit without a confident decision"
                ),
            }
        )

    def _subject_label(self, ctx: _WindowContext, o: _Observed) -> Any:
        """The label stored in the decision log: the omni label, or in a private window the catalog class name
        (AC-PER-43: no model text from a private window is stored)."""
        if ctx.no_capture:
            return o.match.class_name or o.catalog_id
        return o.obj.get("label")

    def _observation_state(self, o: _Observed, observed_at: str) -> dict[str, Any]:
        return {
            "label": o.obj.get("label"),
            "description": o.obj.get("description"),
            "location": o.location,
            "observed_at": observed_at,
            "held": o.held,
        }

    def _choose(self, question: str, options: dict[str, str], state: dict[str, Any], instructions: str):
        try:
            return self.decision.choose(question, options, state=state, instructions=instructions)
        except Exception as exc:  # noqa: BLE001 - decision model is a remote dependency
            return DecisionResult(
                choice=NEW,
                probabilities={},
                needs_review=True,
                error=str(exc),
                model=getattr(self.decision, "model", "") or "unknown",
            )

    def _describe_entity(self, item: dict[str, Any], last_seen: datetime | None, now: datetime) -> str:
        desc = str(item.get("description") or "").strip()
        ago = f" ({int((now - last_seen).total_seconds() // 60)} min ago)" if last_seen else ""
        return (
            f"{item.get('label') or 'entity'}{': ' + desc if desc else ''}, last seen in "
            f"{item.get('last_location') or 'unknown location'} at {_iso(last_seen) if last_seen else 'unknown time'}"
            f"{ago}"
        )

    def _class_items(self, ctx: _WindowContext, catalog_id: str | None) -> dict[str, dict[str, Any]]:
        """AC-PER-30: entity rows (descriptions) of a class, one GET /items per class per window, only on an ask."""
        if not catalog_id:
            return {}
        if catalog_id not in ctx.class_items:
            try:
                rows = self.api.list_items(catalog_id=catalog_id, sort="-last_seen_at", limit=PLACE_ITEMS_LIMIT) or []
            except Exception as exc:  # noqa: BLE001
                logger.warning("listing the entities of a class failed (decision context, person cross-module and cap candidates)", extra={"error": str(exc)})
                rows = []
            ctx.class_items[catalog_id] = {str(r.get("entity_id")): r for r in rows}
        return ctx.class_items[catalog_id]

    def _is_generic(self, o: _Observed) -> bool:
        """AC-PER-60: the instance's class is one of ``config.generic_classes`` (class name or catalog id)."""
        item = self.mapper.item(o.catalog_id)
        if item is None or item.get("category") == "person":
            return False
        names = {normalise_label(str(n)) for n in self.config.generic_classes}
        return normalise_label(str(item.get("class_name") or "")) in names or normalise_label(
            str(item.get("catalog_id") or "")) in names

    def _cross_module(self, a: str | None, b: str | None) -> bool:
        """AC-PER-60: both places lie in a known module and the modules differ (a segment-level place is not)."""
        ma, mb = self.resolver.module_of(a), self.resolver.module_of(b)
        return ma is not None and mb is not None and ma != mb

    def _same_place(self, last_location: str | None, location: str | None) -> bool:
        if last_location is None or location is None:
            return False
        module = self.resolver.module_of(location)
        if module is None:
            return last_location == location
        return self.resolver.module_of(last_location) == module

    def _decide(self, ctx: _WindowContext, o: _Observed, valid: list[_Scored]) -> None:
        cfg = self.config
        if not valid:
            return
        top = valid[0]
        decision_band = band(top.p, cfg.assign_p, cfg.ask_p)
        if decision_band == "assign":
            self._assign(o, top)
            return
        if decision_band == "new":
            return
        options_sc = [s for s in valid if s.p >= cfg.ask_p][:MAX_DECISION_OPTIONS]
        options = {s.entity_id: self._describe_candidate(s) for s in options_sc}
        options[NEW] = f"a different {o.match.class_name or o.obj.get('label') or 'object'} not listed here"
        subject = {
            "camera_id": self.camera_id,
            "observed_at": _iso(ctx.frame_ts(o.frame_idx)),
            "label": self._subject_label(ctx, o),
            "catalog_id": o.catalog_id,
            "location_id": o.location,
            "candidates": [
                {"entity_id": s.entity_id, "p": round(s.p, 4), "similarity": s.candidate.get("similarity")}
                for s in options_sc
            ],
        }
        seen_at = ctx.frame_ts(o.frame_idx)
        details = self._class_items(ctx, o.catalog_id)  # AC-PER-30: descriptions (candidates carry none)
        state = {
            "observation": self._observation_state(o, subject["observed_at"]),
            "candidates": {
                s.entity_id: {
                    "label": s.candidate.get("label"),
                    "description": str(
                        s.candidate.get("description") or (details.get(s.entity_id) or {}).get("description") or ""
                    ),
                    "last_location": s.candidate.get("last_location"),
                    "last_seen_at": str(s.candidate.get("last_seen_at")),
                    "seen_ago_s": _seen_ago_s(seen_at, s.candidate.get("last_seen_at")),
                    "score": round(s.p, 4),
                }
                for s in options_sc
            },
        }
        result = self._choose(SAME_OR_NEW, options, state, "Is the observed object one of the listed entities, or NEW?")
        ctx.log_decision(SAME_OR_NEW, subject, result)
        candidate_ids = [s.entity_id for s in options_sc]
        choice_p = float(result.probabilities.get(result.choice, 0.0)) if result.probabilities else 0.0
        if not result.needs_review and result.choice in candidate_ids and choice_p >= cfg.decision_accept:
            self._assign(o, next(s for s in options_sc if s.entity_id == result.choice))
            return
        if not result.needs_review and result.choice == NEW and choice_p >= cfg.decision_accept:
            return  # confidently a different object
        o.reviews.append(
            {
                "kind": "possible_duplicate",
                "candidate_ids": candidate_ids,
                "probabilities": dict(result.probabilities),
                "description": self._review_description(o, "possible duplicate of an existing entity"),
            }
        )

    def _describe_candidate(self, s: _Scored) -> str:
        c = s.candidate
        return (
            f"{c.get('label') or 'entity'} last seen in {c.get('last_location') or 'unknown location'} at "
            f"{c.get('last_seen_at') or 'unknown time'} (appearance similarity {float(c.get('similarity') or 0):.2f})"
        )

    def _assign(self, o: _Observed, s: _Scored) -> None:
        o.entity_id = s.entity_id
        o.previous_location = s.candidate.get("last_location")

    def _crop(self, ctx: _WindowContext, o: _Observed) -> np.ndarray:
        """Crop from the full-resolution keyframe (AC-PER-21); boxes are normalised, so they scale exactly."""
        image = ctx.window.frames[o.frame_idx].hires
        return crop_image(image, o.bbox) if o.bbox is not None else image

    def _review_description(self, o: _Observed, reason: str) -> str:
        desc = str(o.obj.get("description") or "").strip()
        label = str(o.obj.get("label") or "object").strip()
        return f"{label}: {desc} ({reason})" if desc else f"{label} ({reason})"

    # ------------------------------------------------------------------ payload parts
    def _assignment(self, ctx: _WindowContext, o: _Observed) -> dict[str, Any]:
        has_exemplar = o.embedding is not None
        crop_uri = ctx.crop_uri(o, self._crop(ctx, o)) if has_exemplar else None
        seen_at = ctx.frame_ts(o.frame_idx)
        confidence = _clamp01(o.obj.get("confidence"), 0.5)
        if ctx.hint_conflict:
            confidence = min(confidence, _clamp01(self.config.hint_conflict_confidence, 0.5))
        if ctx.truncated:  # AC-PER-26 salvaged window
            confidence = min(confidence, _clamp01(self.config.omni_truncated_confidence, 0.5))
        if ctx.carried:  # AC-PER-41 module inherited from an earlier window
            confidence = min(confidence, _clamp01(self.config.module_carry_confidence, 0.5))
        sighting: dict[str, Any] = {
            "location_id": o.location,
            "seen_at": _iso(seen_at),
            "bbox": [round(float(v), 6) for v in o.bbox] if o.bbox is not None else None,
            # AC-PER-34: a linked detector box is exact; omni's own box is coarse (the UI draws it as approximate)
            "bbox_source": None if o.bbox is None else ("detector" if o.detection is not None else "omni"),
            "confidence": confidence,
            # AC-PER-43: a sighting in a private window is a location-only fact
            "description": None if ctx.no_capture else str(o.obj.get("description") or "")[:400],
        }
        if o.person_overlap:
            sighting["person_overlap"] = True  # AC-PER-49: the crop was dropped
        regions = ctx.regions(o.frame_idx)
        if regions and not ctx.no_capture:  # AC-PER-49: inventory-api checks any crop against these itself
            if o.bbox is not None:  # review round: the regions that overlap this object survive the cap
                regions = sorted(regions, key=lambda r: not overlaps_person(o.bbox, [r[0]], [r[1]]))
            sent = regions[:MAX_PERSON_BOXES]
            sighting["person_boxes"] = [
                [round(min(max(float(v), 0.0), 1.0), 6) for v in ordered(box)] for box, _ in sent
            ]
            sighting["person_box_kinds"] = [kind for _, kind in sent]
        frame_uri = ctx.frame_uri(o.frame_idx)
        if frame_uri:
            sighting["frame_uri"] = frame_uri
        exemplar: dict[str, Any] | None = None
        if has_exemplar:
            exemplar = {
                "embedding": [float(v) for v in o.embedding],
                "quality": _clamp01(o.detection.score if o.detection else o.obj.get("confidence"), 0.5),
            }
            if crop_uri:
                exemplar["crop_uri"] = crop_uri
        item = self.mapper.item(o.catalog_id)
        is_person = bool(item and item.get("category") == "person")
        a: dict[str, Any] = {
            "ref": o.ref,
            "entity_id": o.entity_id,
            "sighting": sighting,
            # no appearance (biometric-like) data about people from private zones
            "exemplar": None if (is_person and ctx.no_capture) else exemplar,
        }
        if o.entity_id is None:
            attributes = o.obj.get("attributes") if isinstance(o.obj.get("attributes"), dict) else {}
            a["new_entity"] = {
                "catalog_id": o.catalog_id,
                "kind": "person" if is_person else "object",
                "status": "unknown" if o.is_unknown else "provisional",
                "label": (item.get("class_name") if item else None) or str(o.obj.get("label") or "unknown object"),
                "description": None if ctx.no_capture else str(o.obj.get("description") or ""),
                "attributes": attributes,
            }
            if o.is_unknown:
                o.reviews.append(
                    {
                        "kind": "unknown_object",
                        "candidate_ids": [],
                        "probabilities": {},
                        "description": self._review_description(o, "not in the catalog"),
                    }
                )
        for review in o.reviews:
            review.update(
                {
                    "entity_ref": o.ref,
                    # AC-PER-56: an unknown's review shows the whole frame (the UI draws the sighting box on it)
                    "image_uri": (frame_uri or crop_uri) if o.is_unknown else (crop_uri or frame_uri),
                    "observed_at": _iso(seen_at),
                    "location_id": o.location,
                    "model_guess": str(o.obj.get("label") or "") or None,
                }
            )
        return a

    def _expected_count_reviews(self, ctx: _WindowContext, instances: list[_Observed]) -> None:
        live_counts: dict[str, int] = {}
        new_in_window: Counter[str] = Counter()
        for o in instances:
            if o.entity_id is not None or o.catalog_id is None:
                continue
            item = self.mapper.item(o.catalog_id)
            expected = item.get("expected_count") if item else None
            if expected is None:
                continue
            if o.catalog_id not in live_counts:
                live_counts[o.catalog_id] = self._live_count(ctx, o.catalog_id)
            new_in_window[o.catalog_id] += 1
            total = live_counts[o.catalog_id] + new_in_window[o.catalog_id]
            if total > int(expected) and not any(r["kind"] == "possible_duplicate" for r in o.reviews):
                review = {
                    "kind": "possible_duplicate",
                    "candidate_ids": [s.entity_id for s in o.scored[:5]],
                    "probabilities": {},
                    "description": self._review_description(
                        o, f"{total} live '{o.match.class_name}' entities would exceed the expected count of {expected}"
                    ),
                }
                seen_at = ctx.frame_ts(o.frame_idx)
                existing_image = next((r.get("image_uri") for r in o.reviews if r.get("image_uri")), None)
                crop = ctx.crop_uri(o, self._crop(ctx, o)) if o.embedding is not None else None
                image = existing_image or crop or ctx.frame_uri(o.frame_idx)
                review.update(
                    {
                        "entity_ref": o.ref,
                        "image_uri": image,
                        "observed_at": _iso(seen_at),
                        "location_id": o.location,
                        "model_guess": str(o.obj.get("label") or "") or None,
                    }
                )
                o.reviews.append(review)

    def _stock_updates(
        self, ctx: _WindowContext, fungible: list[_Observed], dets: list[list[Detection]]
    ) -> list[dict[str, Any]]:
        if not fungible or ctx.location is None:
            return []
        omni_counts: Counter[tuple[str, int]] = Counter((o.catalog_id, o.frame_idx) for o in fungible)
        classes = list(dict.fromkeys(o.catalog_id for o in fungible))
        det_counts: Counter[tuple[str, int]] = Counter()
        for f, frame_dets in enumerate(dets):
            for det in frame_dets:
                cid = self.mapper.map(det.label).catalog_id
                if cid in classes:
                    det_counts[(cid, f)] += 1
        out = []
        for cid in classes:
            per_frame = [n for (c, _f), n in omni_counts.items() if c == cid]
            per_frame += [n for (c, _f), n in det_counts.items() if c == cid]
            out.append(
                {
                    "catalog_id": cid,
                    "location_id": ctx.location,
                    "est_count": int(max(per_frame)),
                    "seen_at": _iso(ctx.window.t_end),
                }
            )
        return out

    def _visual_events(self, ctx: _WindowContext, obs: dict[str, Any], instances: list[_Observed]) -> list[dict]:
        people = {p.get("ref"): p for p in obs.get("people") or [] if isinstance(p, dict)}
        n_frames = len(ctx.window.frames)
        events = []
        for ev in obs.get("events") or []:
            label = str(ev.get("object_label") or "")
            match = self.mapper.map(label)
            pool = [
                o
                for o in instances
                if (
                    o.match.catalog_id == match.catalog_id
                    if match.catalog_id != UNKNOWN
                    else o.is_unknown and label_key(o.obj.get("label") or "") == label_key(label)  # AC-PER-31
                )
            ]
            if not pool:
                logger.info(
                    "omni event for an object not observed in the window dropped",
                    extra={"window": ctx.window.index, "label": label, "type": ev.get("type")},
                )
                continue
            actor_ref = ev.get("actor_ref")
            target = next((o for o in pool if actor_ref and o.obj.get("held_by") == actor_ref), pool[0])
            fi = ev.get("frame_index")
            fi = fi if isinstance(fi, int) and 0 <= fi < n_frames else n_frames - 1
            actor = people.get(actor_ref) if actor_ref else None
            to_location = self.resolver.resolve(ev.get("to_location_hint"))
            from_location = target.previous_location if target.entity_id else None
            event_type = str(ev.get("type"))
            # AC-PER-51: an event names the place it was seen when the hint gives none (a move only when it differs)
            if to_location is None and target.location is not None and event_type not in LEAVES_INVENTORY and (
                event_type != "moved" or not self._same_area(from_location, target.location)
            ):
                to_location = target.location
            if event_type == "moved" and self._is_person(target) and from_location is not None and (
                to_location is None or self._same_area(from_location, to_location)
            ):
                logger.info(
                    "person track move without a change of place dropped",
                    extra={"window": ctx.window.index, "entity_id": target.entity_id},
                )
                continue
            if event_type == "moved" and self._is_person(target):
                if to_location is None:
                    continue
                event_type = "seen"  # AC-PER-63: a person's own transit is an arrival ("seen" there), never a move
            elif event_type == "moved" and from_location is None:
                # AC-PER-63 (UI5-09): a first sighting is not a move; the new entity itself records where it appeared
                logger.info("moved event without a previous place dropped",
                            extra={"window": ctx.window.index, "entity_ref": target.ref})
                continue
            occurred = ctx.frame_ts(fi)
            if target.entity_id and self._is_repeat(target.entity_id, event_type, self._dedup_place(to_location),
                                                    occurred):
                logger.info(
                    "omni event already recorded from an overlapping window; dropped",
                    extra={"window": ctx.window.index, "entity_id": target.entity_id, "type": ev.get("type")},
                )
                continue
            events.append(
                {
                    "event_type": event_type,
                    "entity_ref": target.ref,
                    # AC-PER-35/36: the people[] descriptor, sanitised; none for people in a private zone
                    # AC-PER-44: a person track's own movement names no actor (only a person handling an object)
                    "actor_desc": (sanitise_text(actor.get("appearance")) or None)
                    if actor and not ctx.no_capture and not self._is_person(target)
                    else None,
                    "from_location": from_location,
                    "to_location": to_location,
                    "occurred_at": _iso(occurred),
                    "confidence": _clamp01(ev.get("confidence"), 0.5),
                    "evidence_uri": ctx.frame_uri(fi),
                    "source": "pipeline",
                }
            )
        return _collapse_window_repeats(events)

    def _module_change_events(
        self, ctx: _WindowContext, instances: list[_Observed], assignments: list[dict[str, Any]], events: list[dict]
    ) -> list[dict]:
        """AC-PER-60 (UI5-03): an object matched to an existing entity whose last place lies in another module gets a
        ``moved`` event (previous place -> this sighting), so a change of module is never silent. Person tracks are
        excluded (AC-PER-63: their own transit is not a move); an omni ``moved`` event of the same instance in this
        window wins; the cross-window dedup applies."""
        if ctx.carried or ctx.truncated or ctx.hint_conflict:
            return []  # review round: never infer a move from an inferred or doubtful place
        confidence = {a["ref"]: a["sighting"]["confidence"] for a in assignments}
        has_move = {e.get("entity_ref") for e in events if e.get("event_type") == "moved"}
        out = []
        for o in instances:
            if not o.entity_id or self._is_person(o) or o.ref in has_move:
                continue
            if not self._cross_module(o.previous_location, o.location):
                continue
            occurred = ctx.frame_ts(o.frame_idx)
            if self._is_repeat(o.entity_id, "moved", self._dedup_place(o.location), occurred):
                continue
            out.append({
                "event_type": "moved",
                "entity_ref": o.ref,
                "actor_desc": None,
                "from_location": o.previous_location,
                "to_location": o.location,
                "occurred_at": _iso(occurred),
                "confidence": confidence.get(o.ref, 0.5),
                "evidence_uri": ctx.frame_uri(o.frame_idx),
                "source": "pipeline",
            })
            logger.info("object matched across modules; moved event emitted",
                        extra={"window": ctx.window.index, "entity_id": o.entity_id,
                               "from": o.previous_location, "to": o.location})
        return out

    def _dedup_place(self, location: str | None) -> str | None:
        """Review round (AC-PER-51): destinations filled from sightings vary with the zone of the box centre, so the
        cross-window dedup key uses the module (or the place itself outside modules)."""
        return self.resolver.module_of(location) or location

    def _is_repeat(self, entity_id: str, event_type: str, to_location: str | None, occurred: datetime) -> bool:
        """True when the same event was recorded within ``event_dedup_s`` (overlapping windows show the same
        action). A repeat extends the dedup horizon, so an event reported by a chain of windows is recorded once."""
        key = (str(entity_id), event_type, to_location)
        previous = self._recent_events.get(key)
        if previous is None or abs((occurred - previous).total_seconds()) > self.config.event_dedup_s:
            return False
        self._recent_events[key] = max(previous, occurred)
        return True

    def _speech_events(
        self, ctx: _WindowContext, transcript: str, instances: list[_Observed]
    ) -> tuple[list[dict], list[dict]]:
        events: list[dict] = []
        claims: list[dict] = []
        seen: set[tuple[str, str]] = set()
        for utterance in split_utterances(transcript):
            act, result = classify_utterance_logged(utterance, self.decision)
            if result is not None:
                subject = {"camera_id": self.camera_id, "t_end": _iso(ctx.window.t_end)}
                if not ctx.no_capture:
                    subject["utterance"] = utterance  # speech from private zones is not stored verbatim
                ctx.log_decision(SPEECH_ACT, subject, result)
            if act != "statement":
                continue
            claim = parse_speech_claim(utterance, self.catalog, self.resolver)
            if claim is None:
                continue
            target = self._claim_target(ctx, claim.catalog_id, instances)
            if target is None:
                logger.info(
                    "speech claim without a known entity ignored",
                    extra={"catalog_id": claim.catalog_id, "location": claim.location_id},
                )
                continue
            key = (target.get("entity_ref") or target["entity_id"], claim.location_id)
            if key in seen:
                continue
            seen.add(key)
            p_statement = result.probabilities.get("statement") if result and not result.needs_review else None
            cap = self.config.speech_max_confidence
            confidence = cap * (_clamp01(p_statement, 0.75) if p_statement is not None else 0.75)
            event = {
                "event_type": claim.event_type,
                "from_location": target.get("last_location"),
                "to_location": claim.location_id,
                "occurred_at": _iso(ctx.window.t_end),
                "confidence": round(min(max(confidence, 0.01), cap), 4),
                "actor_desc": None,
                "evidence_uri": None,
                "source": "speech",
            }
            if target.get("entity_ref"):
                event["entity_ref"] = target["entity_ref"]
            else:
                event["entity_id"] = target["entity_id"]
            events.append(event)
            confirmed_now = any(
                o.ref == target.get("entity_ref") and self.resolver.within(o.location, claim.location_id)
                for o in instances
            )
            claims.append(
                {**target, "location_id": claim.location_id, "at": ctx.window.t_end, "confirmed": confirmed_now}
            )
        return events, claims

    def _claim_target(self, ctx: _WindowContext, catalog_id: str, instances: list[_Observed]) -> dict[str, Any] | None:
        """The entity a spoken claim is about: one sighted in this window, else the most recently seen live one in
        the camera's module (the speaker is near the camera), else the most recently seen live one. Never a new
        entity created from the words alone."""
        for o in instances:
            if o.catalog_id == catalog_id:
                return {"entity_ref": o.ref, "entity_id": o.entity_id, "last_location": o.previous_location}
        try:
            items = self.api.list_items(catalog_id=catalog_id, sort="-last_seen_at", limit=5)
        except Exception as exc:  # noqa: BLE001
            logger.warning("list_items failed for a speech claim", extra={"error": str(exc)})
            return None
        live = [i for i in items if i.get("status") not in ("merged", "dismissed")]
        if not live:
            return None
        module = self.resolver.module_of(ctx.location)
        nearby = [i for i in live if module and self.resolver.module_of(i.get("last_location")) == module]
        chosen = (nearby or live)[0]
        return {
            "entity_ref": None,
            "entity_id": str(chosen["entity_id"]),
            "last_location": chosen.get("last_location"),
        }

    def _check_speech_flags(self, ctx: _WindowContext, instances: list[_Observed]) -> None:
        """Confirm or escalate earlier speech claims on the existing entities sighted in this window. The claim
        state is read from inventory (flag + latest speech ``moved`` event), so it works across camera processes
        and restarts. Only flags without a ``review_reason`` are speech flags: a flag set by an open review item is
        never touched."""
        existing = {o.entity_id: o for o in instances if o.entity_id}
        list_events = getattr(self.api, "list_events", None)
        if not existing or list_events is None:
            return
        try:
            flagged = self.api.list_items(needs_review=True, limit=500)
        except Exception as exc:  # noqa: BLE001
            logger.warning("listing flagged entities failed", extra={"error": str(exc)})
            return
        horizon_s = self.config.speech_confirm_s
        for item in flagged:
            o = existing.get(str(item.get("entity_id")))
            if o is None or item.get("review_reason"):
                continue
            try:
                events = list_events(entity_id=o.entity_id, type="moved", limit=20) or []
            except Exception as exc:  # noqa: BLE001
                logger.warning("listing events failed", extra={"entity_id": o.entity_id, "error": str(exc)})
                continue
            claims = [
                (at, e) for e in events if e.get("source") == "speech" and (at := _parse_ts(e.get("occurred_at")))
            ]
            if not claims:
                continue  # flagged by an operator or for another reason: leave it alone
            claimed_at, claim = max(claims, key=lambda c: c[0])
            claimed_loc = claim.get("to_location")
            seen_at = ctx.frame_ts(o.frame_idx)
            age_s = (seen_at - claimed_at).total_seconds()
            if age_s < 0:
                continue
            if age_s <= horizon_s:
                if self.resolver.within(o.location, claimed_loc):
                    o.clear_claim = claimed_loc
            elif not ctx.no_capture:
                o.reviews.append(
                    {
                        "kind": "conflict",
                        "candidate_ids": [],
                        "probabilities": {},
                        "description": self._review_description(
                            o,
                            f"speech claim 'moved to {claimed_loc}' at {_iso(claimed_at)} was not confirmed by a "
                            f"sighting within {int(horizon_s // 60)} min; now seen in {o.location or 'unknown'}",
                        ),
                    }
                )

    # ------------------------------------------------------------------ after posting
    def _after_post(
        self,
        window: Window,
        payload: dict[str, Any],
        response: dict[str, Any],
        observed: list[_Observed],
        claims: list[dict[str, Any]],
    ) -> None:
        refs = {str(k): str(v) for k, v in (response.get("entities") or {}).items()}

        def resolve(entity_id: str | None, ref: str | None) -> str | None:
            return str(entity_id) if entity_id else refs.get(ref or "")

        # 0. latest appearance texts of person tracks (only sightings actually recorded: none in private windows)
        recorded = {a.get("ref"): a for a in payload.get("assignments", [])}
        for o in observed:
            a = recorded.get(o.ref)
            text = (a or {}).get("sighting", {}).get("description")
            eid = resolve(o.entity_id, o.ref) if a else None
            if eid and text and self._is_person(o):
                self._person_recent.setdefault(eid, deque(maxlen=PERSON_RECENT_DESCRIPTIONS)).append(str(text))

        # 1. remember recorded omni events (overlap dedup)
        for ev in payload.get("events", []):
            if ev.get("source") != "pipeline":
                continue
            eid = resolve(ev.get("entity_id"), ev.get("entity_ref"))
            occurred = _parse_ts(ev.get("occurred_at"))
            if eid and occurred:
                key = (eid, str(ev.get("event_type")), self._dedup_place(ev.get("to_location")))
                self._recent_events[key] = max(self._recent_events.get(key, occurred), occurred)
        horizon = timedelta(seconds=max(self.config.event_dedup_s, 0.0) * 6 + 60)
        for key, at in list(self._recent_events.items()):
            if window.t_end - at > horizon:
                del self._recent_events[key]
        # 2. visual confirmations of earlier speech claims (decided in _check_speech_flags)
        for o in observed:
            if o.entity_id and o.clear_claim:
                self._patch(
                    o.entity_id,
                    {
                        "needs_review": False,
                        "operator": "perception",
                        "note": f"speech claim confirmed by a sighting in {o.clear_claim}",
                    },
                )
        # 3. new speech-only claims: flag until a sighting confirms them
        for claim in claims:
            eid = resolve(claim.get("entity_id"), claim.get("entity_ref"))
            if not eid or claim.get("confirmed"):
                continue
            self._patch(
                eid,
                {
                    "needs_review": True,
                    "operator": "perception",
                    "note": f"speech claim: moved to {claim['location_id']} (unconfirmed)",
                },
            )

    def _patch(self, entity_id: str, fields: dict[str, Any]) -> bool:
        try:
            self.api.patch_item(entity_id, fields)
            return True
        except Exception as exc:  # noqa: BLE001
            logger.warning("patching entity failed", extra={"entity_id": entity_id, "error": str(exc)})
            return False


def _collapse_window_repeats(events: list[dict]) -> list[dict]:
    """AC-PER-10 amendment: one omni reply can list the same action several times (seen live: three identical `used`
    events for one drill, one actor). Within a window, events with the same target, type, destination and actor
    collapse to the most confident one; order of first appearance is kept."""
    best: dict[tuple, dict] = {}
    order: list[tuple] = []
    for ev in events:
        key = (
            str(ev.get("entity_id") or ""),
            str(ev.get("entity_ref") or ""),
            ev.get("event_type"),
            ev.get("to_location"),
            ev.get("actor_desc"),
        )
        if key not in best:
            best[key] = ev
            order.append(key)
        elif float(ev.get("confidence") or 0.0) > float(best[key].get("confidence") or 0.0):
            best[key] = ev
    return [best[k] for k in order]
