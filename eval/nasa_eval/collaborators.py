"""Perception collaborators for the runner (AC-EVAL-5, review round).

``build_collaborators(dataset, target)`` returns the keyword arguments ``perception.replay.run`` needs besides
``source`` / ``camera_id`` / ``start_time`` / ``api`` / ``config``:

- ``local``: deterministic fakes scripted from the gold data, so the pipeline logic (windows, label mapping,
  candidates, gates, events, reviews) is exercised end to end without models:
  - ``GoldDetector`` returns the gold boxes of the frame it is given. It recognises the frame by comparing a small
    thumbnail with the dataset's frames, so it also works on perception's downscaled frames and does not depend on
    the order of calls. Unknown objects are not detected (the detector only knows catalog labels).
  - ``GoldOmni`` answers a window with the gold objects of its frames (catalog class names; a non-catalog label for
    unknowns), the gold events falling in the window (each reported once, with the actor as a person ref) and the
    scripted transcript lines spoken in the window. Profile (``EVAL_OMNI_PROFILE``, default ``repeat_per_frame``):
    ``repeat_per_frame`` lists each object once per frame it appears in, every entry with the same box (its latest
    in the window), exactly like Qwen2.5-Omni-7B did on real ISS footage (cluster integration 2026-10-17: 6 laptops
    + 6 headsets in one window); ``once`` lists each object once at its latest frame; ``truncated`` is
    ``repeat_per_frame`` with a long transcript echo, cut off inside the transcript with finish_reason "length"
    (34 % of cluster replies hit max_tokens, perception AC-PER-26) and interpreted by perception's own
    ``interpret_reply`` (salvage + object cap), so the gate covers salvage.
  - ``gold_asr``: a FakeASR that returns the scripted lines overlapping an audio chunk (each line once); the dataset
    synthesises a tone while a line is spoken so perception's VAD passes it to ASR.
  - ``ThumbnailEmbedder`` (appearance-based, weight-free) / ``MemoryStorage``; the decision model is the real client when ``DECISION_URL`` is set,
    otherwise perception's FakeDecision, which abstains (NEW + review) - the conservative choice: a decision-band
    case then shows up as a duplicate / review in the metrics instead of a silent merge.
- ``cluster``: perception's real clients from the environment (``PerceptionConfig.from_env``: OMNI_URL,
  DECISION_URL required; ASR_URL optional), OWLv2 detector and DINOv2 embedder (lazy model loading), and
  ``MemoryStorage`` (the eval never writes into the deployment's bucket).

``replay_config(dataset)`` adds the manifest's camera zone rectangles to perception's config, so sightings get
zone-level locations (camera-configured zones, ARCHITECTURE decision 5).

``revisit_with_collaborators(dataset, database_url, offset_s, target=...)`` replays the same dataset again, every
timestamp shifted by ``offset_s`` (perception AC-PER-32 revisit). The local fakes map the shifted times back to the
gold, and the gold detector misses every other catalog object (``revisit_misses``): OWLv2 is intermittent on real
footage, so on the revisit half the objects come back omni-only (no exemplar), the case that duplicated on the
cluster.

``bind_inventory_api(database_url)`` starts the inventory-api in-process (FastAPI TestClient) on the fresh eval
database, so perception writes through the real API into exactly that database.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime
from pathlib import Path
from typing import Any

from nasa_eval.dataset import Dataset, GoldObject

log = logging.getLogger(__name__)

TARGETS = ("local", "cluster")
UNKNOWN_LABEL = "unidentified gizmo"  # deliberately far from every catalog name / synonym
THUMB_SIZE = (64, 36)
DETECTION_SCORE = 0.9
OMNI_EVENT_TYPES = ("moved", "added", "removed", "used", "handed_over")  # observation.schema.json events.type
OMNI_PROFILES = ("repeat_per_frame", "once", "truncated")
TRUNCATED_ECHO = "the transcript echo goes on and on " * 40  # verbose model: cut off at max_tokens inside it
DEFAULT_OMNI_PROFILE = "repeat_per_frame"  # how the real omni model answered on ISS footage


def _config_error(message: str) -> Exception:
    from nasa_eval.runner import ConfigError

    return ConfigError(message)


# ------------------------------------------------------------------------------------------------------- labels
def catalog_labels(catalog: Sequence[Mapping[str, Any]] | None = None) -> dict[str, str]:
    """catalog_id -> class_name, from catalog rows (``GET /catalog``) or contracts/db/002_seed.sql."""
    if catalog is not None:
        return {str(c["catalog_id"]): str(c.get("class_name") or c["catalog_id"]) for c in catalog}
    import re

    from nasa_eval import CONTRACTS_DB

    sql = (Path(CONTRACTS_DB) / "002_seed.sql").read_text()
    block = sql.split("INSERT INTO catalog_item", 1)[1].split("ON CONFLICT", 1)[0]
    return {m.group(1): m.group(2) for m in re.finditer(r"^\s*\('([^']+)',\s*'([^']*)'", block, re.MULTILINE)}


def _frame_times(dataset: Dataset) -> list[float]:
    return [i / dataset.fps for i in range(len(dataset.frame_paths()))]


def _appearances_at(gold: Sequence[GoldObject], t: float) -> list[tuple[GoldObject, Any]]:
    return [(g, a) for g in gold for a in g.appearances if abs(a.t - t) < 1e-6]


def _thumb(image: Any) -> Any:
    import numpy as np
    from PIL import Image

    img = Image.fromarray(np.ascontiguousarray(image, dtype=np.uint8)).convert("RGB")
    return np.asarray(img.resize(THUMB_SIZE, Image.BILINEAR), dtype=np.float32)


# ------------------------------------------------------------------------------------------------------- detector
class GoldDetector:
    """``detect(frame, labels)``: the gold (catalog) boxes of the dataset frame closest to ``frame``."""

    def __init__(
        self,
        dataset: Dataset,
        labels: Mapping[str, str],
        *,
        score: float = DETECTION_SCORE,
        miss: set[str] | frozenset[str] = frozenset(),
    ):
        self.dataset = dataset
        self.labels = dict(labels)
        self.score = score
        self.miss = frozenset(miss)  # gold object ids this detector never finds (revisit)
        self._thumbs: list[Any] | None = None
        self.calls = 0

    def _index(self) -> list[Any]:
        if self._thumbs is None:
            import numpy as np
            from PIL import Image

            self._thumbs = []
            for p in self.dataset.frame_paths():
                with Image.open(p) as img:
                    self._thumbs.append(_thumb(np.asarray(img.convert("RGB"))))
        return self._thumbs

    def frame_index(self, frame: Any) -> int:
        import numpy as np

        thumbs = self._index()
        if not thumbs:
            raise ValueError(f"dataset {self.dataset.name} has no frames")
        q = _thumb(frame)
        return int(np.argmin([float(np.abs(t - q).mean()) for t in thumbs]))

    def detect(self, frame: Any, labels: Sequence[str]) -> list[Any]:
        from perception.types import Detection

        self.calls += 1
        t = self.frame_index(frame) / self.dataset.fps
        wanted = set(labels) if labels else None
        out = []
        for g, a in _appearances_at(self.dataset.gold, t):
            if g.is_unknown or g.catalog_id is None or a.bbox is None or g.gold_object_id in self.miss:
                continue
            label = self.labels.get(g.catalog_id, g.catalog_id)
            if wanted is None or label in wanted:
                out.append(Detection(label, tuple(float(v) for v in a.bbox), float(self.score)))
        return out


# ------------------------------------------------------------------------------------------------------- omni
class GoldOmni:
    """``analyze(window, context=None)``: a schema-valid observation built from the gold data of the window."""

    def __init__(
        self,
        dataset: Dataset,
        labels: Mapping[str, str],
        *,
        model: str = "gold-omni",
        profile: str = DEFAULT_OMNI_PROFILE,
        time_offset_s: float = 0.0,
    ):
        if profile not in OMNI_PROFILES:
            raise ValueError(f"unknown GoldOmni profile {profile!r}; expected one of {OMNI_PROFILES}")
        self.profile = profile
        self.time_offset_s = float(time_offset_s)  # a revisit replays the dataset shifted by this
        self.dataset = dataset
        self.labels = dict(labels)
        self.model = model
        self._reported_events: set[tuple[str, int]] = set()
        self._spoken: set[int] = set()
        self._lines = dataset.transcript_lines()
        self.calls = 0

    def _rel(self, ts: datetime) -> float:
        return (ts - self.dataset.manifest.start_time).total_seconds() - self.time_offset_s

    def _label(self, g: GoldObject) -> str:
        if g.is_unknown or g.catalog_id is None:
            return UNKNOWN_LABEL
        return self.labels.get(g.catalog_id, g.catalog_id)

    def analyze(self, window: Any, *, context: str | None = None) -> Any:
        import json

        from perception.schema import validate_observation
        from perception.types import OmniResult

        self.calls += 1
        times = [round(self._rel(kf.timestamp), 6) for kf in window.frames]
        objects: dict[str, dict[str, Any]] = {}
        frames_of: dict[str, list[int]] = {}
        for k, t in enumerate(times):
            for g, a in _appearances_at(self.dataset.gold, t):
                frames_of.setdefault(g.gold_object_id, []).append(k)
                objects[g.gold_object_id] = {  # latest frame of the window wins
                    "label": self._label(g),
                    "description": f"{self._label(g)} in the rack",
                    "bbox": list(a.bbox) if a.bbox is not None else None,
                    "frame_index": k,
                    "confidence": 0.9,
                    "held_by": None,
                }
        listed = list(objects.values())
        if self.profile in ("repeat_per_frame", "truncated"):
            # one entry per frame the object is visible in, all with the same (latest) box: no per-frame localisation
            listed = [
                {**objects[gid], "frame_index": k} for gid in objects for k in frames_of[gid]
            ]
        people: dict[str, str] = {}
        events = []
        lo, hi = (min(times), max(times)) if times else (0.0, -1.0)
        for g in self.dataset.gold:
            for i, e in enumerate(g.events):
                key = (g.gold_object_id, i)
                if (
                    e.type not in OMNI_EVENT_TYPES
                    or key in self._reported_events
                    or not (lo - 1e-6 <= e.t <= hi + 1e-6)
                ):
                    continue
                self._reported_events.add(key)
                ref = None
                if e.actor:
                    ref = next((r for r, d in people.items() if d == e.actor), None) or f"P{len(people) + 1}"
                    people[ref] = e.actor
                k = min(range(len(times)), key=lambda j: abs(times[j] - e.t))
                events.append(
                    {
                        "type": e.type,
                        "object_label": self._label(g),
                        "actor_ref": ref,
                        "to_location_hint": e.to_location,
                        "frame_index": k,
                        "evidence": f"{e.type} seen on camera",
                        "confidence": 0.9,
                    }
                )
        spoken = []
        for j, line in enumerate(self._lines):
            if j not in self._spoken and lo - 1e-6 <= line.t <= hi + 1e-6:
                self._spoken.add(j)
                spoken.append(line.text)
        observation = {
            "scene_summary": f"{len(objects)} objects on the racks",
            "location_hint": None,
            "objects": listed,
            "people": [{"ref": r, "appearance": d, "activity": "handling an item"} for r, d in people.items()],
            "events": events,
            "transcript": " ".join(spoken),
            "audio_events": [],
        }
        if self.profile == "truncated":
            return self._truncated(observation)
        raw = json.dumps(observation)
        errors = validate_observation(observation)
        if errors:  # pragma: no cover - a bug in this fake, surfaced like an invalid model reply
            return OmniResult(status="invalid", raw=raw, model=self.model, error="; ".join(errors))
        return OmniResult(status="ok", observation=observation, raw=raw, model=self.model)


    def _truncated(self, observation: dict[str, Any]) -> Any:
        """The reply a verbose model cut off at max_tokens: compact JSON ending inside the transcript echo."""
        import json

        from perception.omni import interpret_reply
        from perception.types import OmniResult

        verbose = {**observation, "transcript": (observation["transcript"] + " " + TRUNCATED_ECHO).strip()}
        text = json.dumps(verbose, separators=(",", ":"))
        raw = text[: text.index('"transcript":"') + len('"transcript":"') + 40]
        reply = interpret_reply(raw, finish_reason="length")
        if reply.observation is None:  # pragma: no cover - a bug in this fake, surfaced like an invalid model reply
            return OmniResult(status="invalid", raw=raw, model=self.model, error="; ".join(reply.errors),
                              finish_reason="length")
        return OmniResult(status="ok", observation=reply.observation, raw=raw, model=self.model,
                          truncated=reply.truncated, dropped_objects=reply.dropped_objects, finish_reason="length")


# ------------------------------------------------------------------------------------------------------- embedder
class ThumbnailEmbedder:
    """Weight-free appearance embedder for the ``local`` target: the crop resized to a 16x16 RGB thumbnail,
    mean-centred and L2-normalised (16 * 16 * 3 = 768-d, the contract's exemplar size).

    perception's FakeEmbedder keys on the exact dominant colour of perception's painted test rectangles; on the
    synthetic icons after perception's bilinear downscale that colour changes with the sub-pixel position, so a
    moved object looked like a new one. This embedder depends only on what the crop looks like: the same object at
    another position stays >= 0.9, other classes stay low, and identical twins look identical (as they would to a
    real re-identification model), so the pipeline must separate them by its own gates."""

    dim = 768
    side = 16

    def embed(self, crop: Any) -> Any:
        import numpy as np
        from PIL import Image

        arr = np.ascontiguousarray(np.asarray(crop, dtype=np.uint8)[..., :3])
        if arr.ndim != 3 or arr.shape[0] == 0 or arr.shape[1] == 0:
            arr = np.zeros((1, 1, 3), dtype=np.uint8)
        thumb = Image.fromarray(arr).convert("RGB").resize((self.side, self.side), Image.BILINEAR)
        v = np.asarray(thumb, dtype=np.float64).reshape(-1)
        v = v - v.mean()
        norm = float(np.linalg.norm(v))
        if norm == 0.0:  # a uniform crop has no shape; fall back to a fixed unit vector
            v = np.full(self.dim, 1.0 / np.sqrt(self.dim))
        else:
            v = v / norm
        return v.astype(np.float32)


def replay_config(dataset: Dataset, environ: Mapping[str, str] | None = None) -> Any:
    """perception's config from the environment, plus the manifest's camera-configured zones for the dataset's
    camera (AC-PER-11 camera zones) unless ``CAMERA_ZONES`` already configures that camera."""
    _import_perception()
    from perception.config import PerceptionConfig

    cfg = PerceptionConfig.from_env(dict(os.environ if environ is None else environ))
    cam = dataset.manifest.camera
    zones = dataset.manifest.zones
    if zones and cam not in cfg.camera_zones:
        cfg.camera_zones = {**cfg.camera_zones, cam: {k: tuple(float(v) for v in box) for k, box in zones.items()}}
    return cfg


# ------------------------------------------------------------------------------------------------------- ASR
def gold_asr(dataset: Dataset, time_offset_s: float = 0.0) -> Any:
    """perception FakeASR returning the scripted transcript lines whose start falls inside the chunk (once each)."""
    from perception.fakes import FakeASR

    lines = dataset.transcript_lines()
    heard: set[int] = set()
    start = dataset.manifest.start_time

    def transcribe(chunk: Any) -> str:
        t0 = (chunk.timestamp - start).total_seconds() - time_offset_s
        t1 = t0 + chunk.duration_s
        out = []
        for j, line in enumerate(lines):
            if j not in heard and t0 - 1e-6 <= line.t < t1 + 1e-6:
                heard.add(j)
                out.append(line.text)
        return " ".join(out)

    return FakeASR(transcribe, model="gold-asr")


# ------------------------------------------------------------------------------------------------------- factory
def _import_perception() -> Any:
    try:
        import perception  # type: ignore[import-not-found]
        from perception import fakes  # type: ignore[import-not-found]  # noqa: F401
    except ImportError as exc:
        raise _config_error(
            "perception (services/perception, perception.replay.run AC-PER-18) is not importable; install it into "
            "this environment (pip install -e '.[pipeline]') or inject a replay function"
        ) from exc
    return perception


def build_collaborators(
    dataset: Dataset,
    target: str,
    *,
    catalog: Sequence[Mapping[str, Any]] | None = None,
    environ: Mapping[str, str] | None = None,
    revisit_offset_s: float = 0.0,
) -> dict[str, Any]:
    """{omni, detector, embedder, decision, asr, storage} for ``perception.replay.run`` (see module docstring).
    ``revisit_offset_s`` > 0: the collaborators of a revisit replay shifted by that many seconds (local: the gold
    fakes map the times back and the detector misses ``revisit_misses(dataset)``)."""
    if target not in TARGETS:
        raise ValueError(f"unknown target {target!r}; expected one of {TARGETS}")
    _import_perception()
    from perception import fakes
    from perception.config import PerceptionConfig
    from perception.decision import DecisionClient

    env = dict(os.environ if environ is None else environ)
    cfg = PerceptionConfig.from_env(env)
    labels = catalog_labels(catalog)
    if target == "local":
        profile = (env.get("EVAL_OMNI_PROFILE") or DEFAULT_OMNI_PROFILE).strip()
        if profile not in OMNI_PROFILES:
            raise _config_error(f"EVAL_OMNI_PROFILE must be one of {OMNI_PROFILES}, got {profile!r}")
        decision = (
            DecisionClient(cfg.decision_url, model=cfg.decision_model, timeout_s=cfg.decision_timeout_s)
            if cfg.decision_url
            else fakes.FakeDecision()
        )
        revisit = revisit_offset_s > 0
        return {
            "omni": GoldOmni(dataset, labels, profile=profile, time_offset_s=revisit_offset_s),
            "detector": GoldDetector(dataset, labels, miss=revisit_misses(dataset) if revisit else frozenset()),
            "embedder": ThumbnailEmbedder(),
            "decision": decision,
            "asr": gold_asr(dataset, time_offset_s=revisit_offset_s),
            "storage": fakes.MemoryStorage(),
        }
    missing = [var for var, value in (("OMNI_URL", cfg.omni_url), ("DECISION_URL", cfg.decision_url)) if not value]
    if missing:
        raise _config_error(f"target 'cluster' needs {', '.join(missing)} (the deployed model endpoints)")
    from perception.asr import AsrClient
    from perception.detector import OwlV2Detector
    from perception.embedder import Dinov2Embedder
    from perception.omni import create_omni_client

    return {
        "omni": create_omni_client(cfg, catalog_names=sorted(set(labels.values()))),
        "detector": OwlV2Detector(min_score=cfg.min_score),
        "embedder": Dinov2Embedder(),
        "decision": DecisionClient(cfg.decision_url, model=cfg.decision_model, timeout_s=cfg.decision_timeout_s),
        "asr": AsrClient(cfg.asr_url, model=cfg.asr_model) if cfg.asr_url else None,
        "storage": fakes.MemoryStorage(),
    }


# ------------------------------------------------------------------------------------------------------- inventory-api
def _import_inventory_api() -> Any:
    try:
        from fastapi.testclient import TestClient  # type: ignore[import-not-found]
        from inventory_api.config import Settings  # type: ignore[import-not-found]
        from inventory_api.main import create_app  # type: ignore[import-not-found]
    except ImportError as exc:
        raise _config_error(
            "inventory-api (services/inventory-api) is not importable; install it into this environment "
            "(pip install -e '.[pipeline]') so the runner can serve the fresh eval database in-process"
        ) from exc
    return TestClient, Settings, create_app


class _MemoryObjectStore:
    """In-memory ObjectStore (inventory_api.storage protocol) for the in-process API: eval runs never touch S3."""

    def __init__(self, bucket: str = "inventory"):
        self.bucket = bucket
        self.objects: dict[str, tuple[bytes, str]] = {}

    def put(self, key: str, data: bytes, content_type: str) -> str:
        from inventory_api.storage import make_uri, validate_key  # type: ignore[import-not-found]

        self.objects[validate_key(key)] = (bytes(data), content_type)
        return make_uri(self.bucket, key)

    def get(self, key: str) -> Any:
        from inventory_api.storage import ObjectNotFound, StoredObject  # type: ignore[import-not-found]

        if key not in self.objects:
            raise ObjectNotFound(key)
        data, content_type = self.objects[key]
        return StoredObject(iter([data]), content_type, len(data))

    def delete(self, key: str) -> None:
        self.objects.pop(key, None)


def bind_inventory_api(database_url: str) -> tuple[Any, Callable[[], None]]:
    """(HttpInventoryClient over an in-process inventory-api bound to ``database_url``, close function)."""
    TestClient, Settings, create_app = _import_inventory_api()
    _import_perception()
    from perception.inventory_client import HttpInventoryClient

    app = create_app(Settings(database_url=database_url), object_store=_MemoryObjectStore())
    client = TestClient(app)
    client.__enter__()  # run the lifespan (pool open / close)

    def close() -> None:
        client.__exit__(None, None, None)

    return HttpInventoryClient(str(client.base_url), http_client=client), close


def revisit_misses(dataset: Dataset) -> frozenset[str]:
    """Catalog gold objects the local detector misses on the revisit: every other one, by sorted gold id."""
    catalog = sorted(g.gold_object_id for g in dataset.gold if not g.is_unknown and g.catalog_id is not None)
    return frozenset(catalog[1::2])


def revisit_with_collaborators(dataset: Dataset, database_url: str, offset_s: float, *, target: str) -> Any:
    """The revisit pass (perception AC-PER-32): the same dataset replayed into the same database, shifted by
    ``offset_s`` seconds."""
    if offset_s <= 0:
        raise ValueError("a revisit needs a positive time offset")
    return replay_with_collaborators(dataset, database_url, target=target, time_offset_s=offset_s)


def replay_with_collaborators(dataset: Dataset, database_url: str, *, target: str, time_offset_s: float = 0.0) -> Any:
    """Bind the API to ``database_url``, build the collaborators for ``target`` and run perception.replay
    (every timestamp shifted by ``time_offset_s``)."""
    _import_perception()
    from datetime import timedelta

    from perception import replay

    shift = timedelta(seconds=time_offset_s)
    api, close = bind_inventory_api(database_url)
    try:
        collab = build_collaborators(dataset, target, catalog=api.get_catalog(), revisit_offset_s=time_offset_s)
        stats = replay.run(
            ((ts + shift, frame, chunk) for ts, frame, chunk in dataset.iter_source()),
            camera_id=dataset.manifest.camera,
            start_time=dataset.manifest.start_time + shift,
            api=api,
            config=replay_config(dataset),
            **collab,
        )
        storage = collab["storage"]
        log.info("replay done dataset=%s stored_objects=%d", dataset.name, len(getattr(storage, "objects", {})))
        return stats
    finally:
        close()
