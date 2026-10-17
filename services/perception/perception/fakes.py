"""Deterministic fakes for every collaborator (AC-PER-18), used by tests, eval and smoke runs.

Synthetic scenes: ``make_frame`` paints each object id as a uniform rectangle of ``object_color(object_id)``.
``FakeDetector(objects={object_id: label})`` finds those rectangles by colour; ``FakeEmbedder`` maps a crop to a
vector seeded by the crop's dominant colour, so the same object gives the same vector at any position/size and
``FakeEmbedder().vector_for_object(object_id)`` predicts it.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Mapping, Sequence
from typing import Any

import numpy as np

from . import NEW
from .detector import DEFAULT_MIN_SCORE, filter_detections
from .schema import empty_observation, parse_observation, validate_observation
from .types import AsrResult, AudioChunk, DecisionResult, Detection, OmniResult, Window

DEFAULT_BACKGROUND = (40, 40, 40)


def _digest(text: str) -> bytes:
    return hashlib.sha256(text.encode("utf-8")).digest()


def object_color(object_id: str) -> tuple[int, int, int]:
    """Deterministic (hashlib-based) RGB colour for an object id, never equal to the background colour."""
    d = _digest(f"object-color:{object_id}")
    # Components in 64..255: always brighter than the default background (40, 40, 40).
    return (64 + d[0] % 192, 64 + d[1] % 192, 64 + d[2] % 192)


def _pixel_box(bbox: Sequence[float], height: int, width: int) -> tuple[int, int, int, int]:
    x1, y1, x2, y2 = (float(v) for v in bbox)
    px1 = min(max(round(x1 * width), 0), width)
    px2 = min(max(round(x2 * width), 0), width)
    py1 = min(max(round(y1 * height), 0), height)
    py2 = min(max(round(y2 * height), 0), height)
    return px1, py1, px2, py2


def make_frame(
    objects: Sequence[tuple[str, tuple[float, float, float, float]]] = (),
    *,
    height: int = 480,
    width: int = 640,
    background: tuple[int, int, int] = DEFAULT_BACKGROUND,
) -> np.ndarray:
    """HxWx3 uint8 frame with each (object_id, normalised bbox) painted in ``object_color(object_id)``."""
    frame = np.empty((height, width, 3), dtype=np.uint8)
    frame[:, :] = background
    for object_id, bbox in objects:
        px1, py1, px2, py2 = _pixel_box(bbox, height, width)
        if px2 > px1 and py2 > py1:
            frame[py1:py2, px1:px2] = object_color(object_id)
    return frame


class FakeOmni:
    """Scripted omni model: call i returns ``observations[i]``: dict -> validated observation (status ok, or
    "invalid" if schema-invalid); str -> raw reply parsed like the real client; None -> status "error". Calls past
    the end return a valid empty observation. ``calls`` records the windows received."""

    def __init__(self, observations: Sequence[dict[str, Any] | str | None] = (), *, model: str = "fake-omni"):
        self.observations = list(observations)
        self.model = model
        self.calls: list[Window] = []

    def analyze(self, window: Window, *, context: str | None = None) -> OmniResult:
        i = len(self.calls)
        self.calls.append(window)
        if i >= len(self.observations):
            obs = empty_observation()
            return OmniResult(status="ok", observation=obs, raw=json.dumps(obs), model=self.model)
        scripted = self.observations[i]
        if scripted is None:
            return OmniResult(status="error", model=self.model, error="scripted omni failure")
        if isinstance(scripted, str):
            obs, errors = parse_observation(scripted)
            if obs is None:
                return OmniResult(status="invalid", raw=scripted, model=self.model, error="; ".join(errors))
            return OmniResult(status="ok", observation=obs, raw=scripted, model=self.model)
        raw = json.dumps(scripted)
        errors = validate_observation(scripted)
        if errors:
            return OmniResult(status="invalid", raw=raw, model=self.model, error="; ".join(errors))
        return OmniResult(status="ok", observation=json.loads(raw), raw=raw, model=self.model)


class FakeDetector:
    """``objects``: {object_id: label} -> detect painted rectangles by colour (score ``score``).
    ``script``: list (call i -> script[i], [] past the end) or callable(frame, labels) -> detections.
    Detections below ``min_score`` are dropped."""

    def __init__(
        self,
        objects: Mapping[str, str] | None = None,
        *,
        script: Sequence[Sequence[Detection]]
        | Callable[[np.ndarray, Sequence[str]], Sequence[Detection]]
        | None = None,
        score: float = 0.9,
        min_score: float = DEFAULT_MIN_SCORE,
    ):
        self.objects = dict(objects or {})
        self.script = script
        self.score = score
        self.min_score = min_score
        self.calls: list[tuple[np.ndarray, list[str]]] = []

    def detect(self, frame: np.ndarray, labels: Sequence[str]) -> list[Detection]:
        i = len(self.calls)
        self.calls.append((frame, list(labels)))
        if callable(self.script):
            found = list(self.script(frame, labels))
        elif self.script is not None:
            found = list(self.script[i]) if i < len(self.script) else []
        else:
            found = self._find_painted(frame)
        return filter_detections(found, self.min_score)

    def _find_painted(self, frame: np.ndarray) -> list[Detection]:
        h, w = frame.shape[:2]
        out = []
        for object_id, label in self.objects.items():
            mask = np.all(frame == np.asarray(object_color(object_id), dtype=np.uint8), axis=-1)
            if not mask.any():
                continue
            ys = np.flatnonzero(mask.any(axis=1))
            xs = np.flatnonzero(mask.any(axis=0))
            bbox = (xs[0] / w, ys[0] / h, (xs[-1] + 1) / w, (ys[-1] + 1) / h)
            out.append(Detection(label, tuple(float(v) for v in bbox), float(self.score)))
        return out


class FakeEmbedder:
    """768-d L2-normalised float32 vectors seeded (hashlib) by the crop's dominant colour."""

    dim = 768

    def __init__(self, dim: int = 768):
        self.dim = dim

    def embed(self, crop: np.ndarray) -> np.ndarray:
        return self.vector_for_color(dominant_color(crop))

    def vector_for_color(self, color: tuple[int, int, int]) -> np.ndarray:
        seed = int.from_bytes(_digest("embed:{},{},{}".format(*(int(c) for c in color)))[:8], "big")
        v = np.random.default_rng(seed).normal(size=self.dim)
        return (v / np.linalg.norm(v)).astype(np.float32)

    def vector_for_object(self, object_id: str) -> np.ndarray:
        """== embed(any crop filled with object_color(object_id))."""
        return self.vector_for_color(object_color(object_id))


def dominant_color(image: np.ndarray, background: tuple[int, int, int] = DEFAULT_BACKGROUND) -> tuple[int, int, int]:
    """Most frequent exact RGB colour of ``image``, ignoring the synthetic background when anything else is there."""
    pixels = np.asarray(image, dtype=np.uint8).reshape(-1, 3).astype(np.uint32)
    if pixels.size == 0:
        return background
    packed = (pixels[:, 0] << 16) | (pixels[:, 1] << 8) | pixels[:, 2]
    values, counts = np.unique(packed, return_counts=True)
    bg = (background[0] << 16) | (background[1] << 8) | background[2]
    if len(values) > 1:
        counts = np.where(values == bg, -1, counts)
    best = int(values[int(np.argmax(counts))])
    return ((best >> 16) & 255, (best >> 8) & 255, best & 255)


def _failure(model: str, error: str) -> DecisionResult:
    return DecisionResult(choice=NEW, probabilities={}, model=model, needs_review=True, error=error)


class FakeDecision:
    """``script``: callable(question, options, state) -> DecisionResult | dict | Exception, or a list consumed in
    order. dict form: {"choice": ..., "probabilities": {...}}. An Exception (returned or raised) behaves like a
    client failure: DecisionResult(choice="NEW", probabilities={}, needs_review=True). No script / exhausted list ->
    the same failure result. ``calls`` records (question, options, state)."""

    def __init__(self, script: Any = None, *, model: str = "fake-decision", latency_ms: int = 5):
        self.script = script
        self.model = model
        self.latency_ms = latency_ms
        self.calls: list[tuple[str, Any, Any]] = []
        self._next = 0

    def choose(
        self, question: str, options: Any, *, state: dict[str, Any] | None = None, instructions: str | None = None
    ) -> DecisionResult:
        self.calls.append((question, options, state))
        try:
            if callable(self.script):
                result = self.script(question, options, state)
            elif isinstance(self.script, Sequence) and not isinstance(self.script, str | bytes):
                if self._next >= len(self.script):
                    return _failure(self.model, "decision script exhausted")
                result = self.script[self._next]
                self._next += 1
            else:
                return _failure(self.model, "no decision script")
        except Exception as exc:  # noqa: BLE001 - a raising script behaves like a failing client
            return _failure(self.model, f"{type(exc).__name__}: {exc}")
        if isinstance(result, BaseException):
            return _failure(self.model, f"{type(result).__name__}: {result}")
        if isinstance(result, DecisionResult):
            if not result.model:
                result.model = self.model
            return result
        if isinstance(result, Mapping):
            return DecisionResult(
                choice=str(result["choice"]),
                probabilities={str(k): float(v) for k, v in (result.get("probabilities") or {}).items()},
                latency_ms=int(result.get("latency_ms", self.latency_ms)),
                model=str(result.get("model", self.model)),
                needs_review=bool(result.get("needs_review", False)),
            )
        return _failure(self.model, f"unsupported scripted decision {result!r}")


class FakeASR:
    """``transcripts``: list (call i -> text; None -> unavailable, ok=False; past the end -> ok with text "") or
    callable(chunk) -> str | None. ``available=False`` -> every call ok=False. ``calls`` records (chunk, context)."""

    def __init__(
        self,
        transcripts: Sequence[str | None] | Callable[[AudioChunk], str | None] = (),
        *,
        available: bool = True,
        model: str = "fake-asr",
    ):
        self.transcripts = transcripts
        self.available = available
        self.model = model
        self.calls: list[tuple[AudioChunk, str]] = []

    def transcribe(self, chunk: AudioChunk, *, context: str = "") -> AsrResult:
        i = len(self.calls)
        self.calls.append((chunk, context))
        if not self.available:
            return AsrResult(text="", ok=False, model=self.model, error="ASR unavailable")
        if callable(self.transcripts):
            text = self.transcripts(chunk)
        elif i < len(self.transcripts):
            text = self.transcripts[i]
        else:
            text = ""
        if text is None:
            return AsrResult(text="", ok=False, model=self.model, error="scripted ASR failure")
        return AsrResult(text=str(text), ok=True, model=self.model)


class MemoryStorage:
    """In-memory object store: ``objects[key] = bytes``, ``content_types[key] = str``; URIs ``s3://bucket/key``."""

    def __init__(self, bucket: str = "inventory"):
        self.bucket = bucket
        self.objects: dict[str, bytes] = {}
        self.content_types: dict[str, str] = {}

    def put(self, key: str, data: bytes, content_type: str) -> str:
        self.objects[key] = bytes(data)
        self.content_types[key] = content_type
        return f"s3://{self.bucket}/{key}"
