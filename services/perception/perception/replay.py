"""AC-PER-18 public replay interface (used by eval and smoke tests).

``run(source, *, camera_id, start_time, api, omni, detector, embedder, decision, asr, storage, config)``:
  * ``source``: a video path/URL (ingested with ffmpeg) or an iterable of ``(timestamp, frame_ndarray,
    audio_chunk | None)`` where ``audio_chunk`` is an AudioChunk or a 1-D 16 kHz mono numpy array (int16, or float
    in [-1, 1]) starting at ``timestamp``. An item's audio is pushed to the window builder before its frame.
  * per window: detector on every frame (labels = catalog class names), omni ``analyze``, ASR on the window audio
    unless silent (VAD RMS gate), transcript choice (ASR text; omni transcript only when ASR is unavailable),
    transcript cleaning, Reconciler.process.
  * ``asr`` may be None (no ASR endpoint configured).

An audio chunk usually overlaps several windows; it is transcribed (and its transcript stored and mined for speech
claims) only with the first window it is attached to, so speech is never recorded twice. ASR does not depend on the
omni model: when omni fails for a window, its audio is still transcribed and the transcript is carried into the next
window that is written.

The reconciler reloads catalog and locations every ``config.catalog_refresh_s`` (``clock``, default
``time.monotonic``); the detector labels, the omni prompt's class names and the ASR context follow.
"""

from __future__ import annotations

import logging
from collections import OrderedDict
from collections.abc import Callable, Iterable, Iterator
from datetime import datetime
from typing import Any

import numpy as np

from .asr import build_asr_context
from .config import PerceptionConfig
from .ingest import ingest, redact_source
from .media import to_pcm16
from .reconciler import Reconciler
from .speech import clean_transcript, is_silent
from .types import AudioChunk, Detection, Keyframe, ReplayStats, Window
from .window import WindowBuilder

logger = logging.getLogger(__name__)

DETECTION_CACHE_SIZE = 64


def _iter_items(
    source: str | Iterable[tuple[datetime, Any, Any]],
    *,
    start_time: datetime,
    config: PerceptionConfig,
    loop: bool,
    max_loops: int | None,
) -> Iterator[Keyframe | AudioChunk]:
    if isinstance(source, str):
        yield from ingest(
            source,
            start_time=start_time,
            fps=config.fps,
            chunk_s=config.audio_chunk_s,
            sample_rate=config.audio_sample_rate,
            loop=loop,
            max_loops=max_loops,
        )
        return
    for index, item in enumerate(source):
        timestamp, frame, audio = item
        if audio is not None:
            if isinstance(audio, AudioChunk):
                yield audio
            else:
                yield AudioChunk(
                    timestamp=timestamp, samples=to_pcm16(np.asarray(audio)), sample_rate=config.audio_sample_rate
                )
        if frame is not None:
            pts = (timestamp - start_time).total_seconds()
            yield Keyframe(timestamp=timestamp, image=np.asarray(frame, dtype=np.uint8), pts=pts, index=index)


class _Pipeline:
    def __init__(
        self,
        *,
        camera_id: str,
        api: Any,
        omni: Any,
        detector: Any,
        embedder: Any,
        decision: Any,
        asr: Any,
        storage: Any,
        config: PerceptionConfig,
        clock: Callable[[], float] | None = None,
    ):
        self.camera_id = camera_id
        self.omni = omni
        self.detector = detector
        self.asr = asr
        self.config = config
        self.stats = ReplayStats()
        catalog = list(api.get_catalog())
        locations = list(api.get_locations())
        self._fill_omni_names = getattr(omni, "catalog_names", None) == []  # built before the catalog was known (CLI)
        self._use_catalog(catalog, locations)
        self.reconciler = Reconciler(
            api=api,
            embedder=embedder,
            decision=decision,
            storage=storage,
            config=config,
            camera_id=camera_id,
            catalog=catalog,
            locations=locations,
            clock=clock,
        )
        self._catalog_version = self.reconciler.catalog_version
        self._carried: list[str] = []  # transcripts of windows that could not be written (omni failed)
        self._detections: OrderedDict[tuple[datetime, int], list[Detection]] = OrderedDict()
        self._transcribed: set[tuple[datetime, int]] = set()

    def _use_catalog(self, catalog: list[dict[str, Any]], locations: list[dict[str, Any]]) -> None:
        self.labels = sorted({c["class_name"] for c in catalog if c.get("class_name")})
        if self._fill_omni_names:
            self.omni.catalog_names = list(self.labels)
        self.asr_context = build_asr_context(catalog, locations)

    def refresh(self) -> None:
        self.reconciler.refresh_if_stale()
        if self.reconciler.catalog_version != self._catalog_version:
            self._catalog_version = self.reconciler.catalog_version
            self._use_catalog(self.reconciler.catalog, self.reconciler.locations)
            self._detections.clear()

    def detect(self, frame: Keyframe) -> list[Detection]:
        key = (frame.timestamp, frame.index)
        if key not in self._detections:
            # AC-PER-21: detect on the full-resolution keyframe (omni gets the downscaled ``image``)
            self._detections[key] = list(self.detector.detect(frame.hires, self.labels))
            while len(self._detections) > DETECTION_CACHE_SIZE:
                self._detections.popitem(last=False)
        return self._detections[key]

    def transcript(self, window: Window, omni_transcript: str) -> str:
        """The transcript to store: ASR text; the omni transcript only when ASR is missing/unavailable; '' on
        silence, for hallucinations, and for audio already transcribed with an earlier window."""
        cfg = self.config
        chunk = window.audio
        silent = is_silent(chunk, cfg.vad_rms)
        if chunk is not None:
            key = (chunk.timestamp, len(chunk.samples))
            if key in self._transcribed:
                return ""
            self._transcribed.add(key)
        if silent:
            return ""
        text: str | None = omni_transcript
        if self.asr is not None:
            self.stats.asr_calls += 1
            try:
                result = self.asr.transcribe(chunk, context=self.asr_context)
            except Exception as exc:  # noqa: BLE001 - ASR is optional; fall back to omni
                logger.warning("ASR raised; using omni transcript", extra={"error": str(exc)})
                result = None
            if result is not None and result.ok:
                text = result.text
        return clean_transcript(text, silent=False, max_repeats=cfg.hallucination_repeats)

    def window(self, window: Window) -> None:
        self.stats.windows += 1
        try:
            self.refresh()
            detections = [self.detect(f) for f in window.frames]
            result = self.omni.analyze(window)
            if not result.ok:
                self.stats.invalid += 1
                logger.info(
                    "omni window invalid",
                    extra={"window": window.index, "status": result.status, "error": result.error},
                )
                carried = self.transcript(window, "")  # ASR does not depend on omni: keep the speech
                if carried:
                    self._carried.append(carried)
                return
            omni_transcript = str((result.observation or {}).get("transcript") or "")
            transcript = " ".join(t for t in [*self._carried, self.transcript(window, omni_transcript)] if t)
            outcome = self.reconciler.process(window, result, detections, transcript=transcript, model=result.model)
            if outcome.status == "posted":
                self._carried.clear()
        except Exception:
            self.stats.errors += 1
            logger.exception("window processing failed", extra={"window": window.index})
            return
        if outcome.status == "posted":
            self.stats.posted += 1
            self.stats.decisions += len((outcome.payload or {}).get("decisions") or [])
        elif outcome.status == "invalid":
            self.stats.invalid += 1
        else:
            self.stats.errors += 1


def run(
    source: str | Iterable[tuple[datetime, Any, Any]],
    *,
    camera_id: str,
    start_time: datetime,
    api: Any,
    omni: Any,
    detector: Any,
    embedder: Any,
    decision: Any,
    asr: Any,
    storage: Any,
    config: PerceptionConfig | None = None,
    loop: bool = False,
    max_loops: int | None = None,
    clock: Callable[[], float] | None = None,
) -> ReplayStats:
    """Run the whole perception pipeline over ``source`` and return counters. ``loop``/``max_loops`` apply to video
    sources only; ``clock`` drives the catalog refresh (default ``time.monotonic``)."""
    config = config or PerceptionConfig()
    pipeline = _Pipeline(
        camera_id=camera_id,
        api=api,
        omni=omni,
        detector=detector,
        embedder=embedder,
        decision=decision,
        asr=asr,
        storage=storage,
        config=config,
        clock=clock,
    )
    builder = WindowBuilder(
        n_frames=config.window_frames,
        stride_s=config.stride_s,
        long_side=config.long_side,
        detect_long_side=config.detect_long_side,
    )
    logger.info(
        "replay started",
        extra={"camera_id": camera_id, "source": redact_source(source) if isinstance(source, str) else "<iterable>"},
    )
    for item in _iter_items(source, start_time=start_time, config=config, loop=loop, max_loops=max_loops):
        if isinstance(item, AudioChunk):
            pipeline.stats.audio_chunks += 1
        else:
            pipeline.stats.frames += 1
        for window in builder.push(item):
            pipeline.window(window)
    for window in builder.flush():
        pipeline.window(window)
    logger.info("replay finished", extra={"camera_id": camera_id, **pipeline.stats.__dict__})
    return pipeline.stats
