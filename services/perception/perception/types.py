"""Plain data types shared by the perception modules (no behaviour)."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

import numpy as np


@dataclass(frozen=True)
class Keyframe:
    """One decoded keyframe. ``image`` is HxWx3 uint8 RGB (in a window: downscaled for omni, AC-PER-2).
    ``detect_image`` (AC-PER-21) is the same frame at detection resolution (original capped at
    ``detect_long_side``), or None when it would equal ``image``. ``timestamp`` = start_time + ``pts`` (seconds)."""

    timestamp: datetime
    image: np.ndarray
    pts: float = 0.0
    index: int = 0
    detect_image: np.ndarray | None = None

    @property
    def hires(self) -> np.ndarray:
        """The image for the detector, crops and stored evidence frames: ``detect_image`` if set, else ``image``."""
        return self.detect_image if self.detect_image is not None else self.image


@dataclass(frozen=True)
class AudioChunk:
    """Mono PCM16 audio starting at ``timestamp``. ``samples`` is a 1-D int16 array at ``sample_rate`` Hz."""

    timestamp: datetime
    samples: np.ndarray
    sample_rate: int = 16000

    @property
    def duration_s(self) -> float:
        return len(self.samples) / float(self.sample_rate)

    @property
    def end(self) -> datetime:
        return self.timestamp + timedelta(seconds=self.duration_s)


@dataclass(frozen=True)
class Window:
    """An omni observation window: the last N keyframes plus the overlapping audio chunk (or None)."""

    t_start: datetime
    t_end: datetime
    frames: list[Keyframe]
    audio: AudioChunk | None = None
    index: int = 0


@dataclass(frozen=True)
class Detection:
    """Detector output. ``bbox`` = (x1, y1, x2, y2) normalised to 0..1 on the frame."""

    label: str
    bbox: tuple[float, float, float, float]
    score: float


@dataclass
class OmniResult:
    """Result of one omni call. ``status`` is "ok" (``observation`` validated against the schema), "invalid"
    (unparseable / schema-invalid after one repair retry) or "error" (transport / HTTP failure)."""

    status: str
    observation: dict[str, Any] | None = None
    raw: str = ""
    model: str = ""
    attempts: int = 1
    latency_ms: int = 0
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.status == "ok"


@dataclass
class DecisionResult:
    """Decision-model read. On timeout/error: ``choice == "NEW"``, ``probabilities == {}``, ``needs_review`` True."""

    choice: str
    probabilities: dict[str, float] = field(default_factory=dict)
    latency_ms: int = 0
    model: str = ""
    needs_review: bool = False
    error: str | None = None


@dataclass
class AsrResult:
    """ASR transcription. ``ok`` False when the endpoint was unavailable or failed."""

    text: str
    ok: bool = True
    model: str = ""
    latency_ms: int = 0
    error: str | None = None


@dataclass(frozen=True)
class LabelMatch:
    """Label -> catalog mapping. ``catalog_id`` is a catalog id or ``perception.UNKNOWN``.
    ``method`` in {"exact", "synonym", "fuzzy", "embedding", "none"}."""

    catalog_id: str
    score: float
    method: str
    class_name: str | None = None


@dataclass
class ReplayStats:
    """Summary of a replay run."""

    frames: int = 0
    audio_chunks: int = 0
    windows: int = 0
    posted: int = 0
    invalid: int = 0
    errors: int = 0
    asr_calls: int = 0
    decisions: int = 0
