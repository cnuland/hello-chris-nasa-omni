"""AC-PER-2 window builder.

Semantics (pinned by the tests):
  * ``push(item)`` accepts Keyframe or AudioChunk (in timestamp order) and returns the windows that became ready.
  * A window is emitted on the first frame, and then on every frame whose timestamp is at least ``stride_s`` after the
    previous window's ``t_end``. It contains the last ``n_frames`` keyframes up to and including that frame,
    downscaled to ``long_side`` (never upscaled).
  * AC-PER-21: each keyframe also keeps ``detect_image`` = the original capped at ``detect_long_side`` (default
    1280; 0 = none; never smaller than the omni image). No copy is made when no resize is needed, and
    ``detect_image`` is None when it would be the omni image itself. Memory: the builder retains at most
    ``n_frames`` keyframes, i.e. at most ``n_frames * detect_long_side**2 * 3`` bytes of hires pixels.
  * ``t_start`` = first frame timestamp, ``t_end`` = last frame timestamp.
  * ``audio`` is the received chunk with the largest overlap with [t_start, t_end] (a chunk covering a zero-length
    window counts if it contains the instant); None if no chunk overlaps.
  * ``flush()`` emits a final window when frames arrived after the last emitted window.
"""

from __future__ import annotations

import dataclasses
from collections import deque
from datetime import datetime

from .media import downscale
from .types import AudioChunk, Keyframe, Window

_EPS_S = 1e-6
MAX_AUDIO_CHUNKS = 32


class WindowBuilder:
    def __init__(self, n_frames: int = 6, stride_s: float = 4.0, long_side: int = 448, detect_long_side: int = 1280):
        if n_frames < 1:
            raise ValueError("n_frames must be >= 1")
        self.n_frames = n_frames
        self.stride_s = stride_s
        self.long_side = long_side
        self.detect_long_side = detect_long_side
        self._frames: deque[Keyframe] = deque(maxlen=n_frames)
        self._audio: deque[AudioChunk] = deque(maxlen=MAX_AUDIO_CHUNKS)
        self._last_end: datetime | None = None
        self._pending = False
        self._index = 0

    def push(self, item: Keyframe | AudioChunk) -> list[Window]:
        if isinstance(item, AudioChunk):
            self._audio.append(item)
            return []
        if not isinstance(item, Keyframe):
            raise TypeError(f"expected Keyframe or AudioChunk, got {type(item).__name__}")
        frame = self._prepare(item)
        self._frames.append(frame)
        self._pending = True
        if self._last_end is None or (frame.timestamp - self._last_end).total_seconds() >= self.stride_s - _EPS_S:
            return [self._emit()]
        return []

    def flush(self) -> list[Window]:
        if not self._pending or not self._frames:
            return []
        return [self._emit()]

    # ------------------------------------------------------------------ internals
    def _prepare(self, item: Keyframe) -> Keyframe:
        image = downscale(item.image, self.long_side)
        detect = None
        if self.detect_long_side > 0:
            cap = max(self.detect_long_side, self.long_side) if self.long_side > 0 else self.detect_long_side
            detect = downscale(item.image, cap)
            if detect is image or detect.shape == image.shape:
                detect = None  # identical to the omni image: keep one array
        return dataclasses.replace(item, image=image, detect_image=detect)

    def _emit(self) -> Window:
        frames = list(self._frames)
        t_start, t_end = frames[0].timestamp, frames[-1].timestamp
        window = Window(
            t_start=t_start, t_end=t_end, frames=frames, audio=self._best_audio(t_start, t_end), index=self._index
        )
        self._index += 1
        self._last_end = t_end
        self._pending = False
        self._prune_audio(t_start)
        return window

    def _best_audio(self, t_start: datetime, t_end: datetime) -> AudioChunk | None:
        best, best_overlap = None, 0.0
        for chunk in self._audio:
            c_start, c_end = chunk.timestamp, chunk.end
            if t_start == t_end:
                overlap = 1.0 if c_start <= t_start < c_end else 0.0
            else:
                overlap = (min(c_end, t_end) - max(c_start, t_start)).total_seconds()
            if overlap > best_overlap:
                best, best_overlap = chunk, overlap
        return best

    def _prune_audio(self, t_start: datetime) -> None:
        # Keep chunks that may still overlap a future window (future windows never start before ``t_start``).
        while self._audio and self._audio[0].end <= t_start and len(self._audio) > 1:
            self._audio.popleft()
