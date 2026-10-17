"""AC-PER-1 ingest: read a video source (file path, looping allowed, or URL) with ffmpeg.

Keyframes are produced at ``fps`` (default 1) and audio as 16 kHz mono PCM16 chunks of ``chunk_s`` seconds
(default 5). Timestamps are wall-clock: ``start_time + media pts``. For loops the pts keeps increasing across
iterations (each pass is offset by the media duration), so timestamps stay monotonic.

Implementation: the ffmpeg binary shipped with imageio-ffmpeg is run as a subprocess. A probe (``ffmpeg -i``) reads
the stream layout first; then one process decodes video (``-vf fps=N``, raw RGB24 on stdout) and another decodes
audio (``-ac 1 -ar 16000 -f s16le``). ``ingest`` merges both streams in timestamp order.
"""

from __future__ import annotations

import contextlib
import heapq
import logging
import os
import re
import subprocess
import tempfile
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import numpy as np

from .types import AudioChunk, Keyframe

logger = logging.getLogger(__name__)

PROBE_TIMEOUT_S = 30.0
MIN_TAIL_S = 0.05  # audio tails shorter than this (encoder padding) are dropped
MAX_SPOOL_BYTES = int(os.environ.get("PERCEPTION_MAX_SPOOL_MB", "2048")) * 1024 * 1024


class IngestError(RuntimeError):
    """The source could not be opened or decoded."""


@dataclass(frozen=True)
class MediaInfo:
    width: int | None
    height: int | None
    has_video: bool
    has_audio: bool
    duration_s: float | None


def ffmpeg_exe() -> str:
    import imageio_ffmpeg

    return imageio_ffmpeg.get_ffmpeg_exe()


def _is_url(source: str) -> bool:
    return bool(re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*://", source))


def redact_source(source: str) -> str:
    """``source`` safe for logs and error messages: URL userinfo (user:password@), query string (tokens, presigned
    signatures) and fragment removed; file paths unchanged."""
    text = str(source)
    if not _is_url(text):
        return text
    try:
        parts = urlsplit(text)
        netloc = parts.netloc.rpartition("@")[2]
        return urlunsplit((parts.scheme, netloc, parts.path, "", ""))
    except ValueError:
        return text.split("://", 1)[0] + "://<redacted>"


def _scrub(text: str, source: str) -> str:
    """Replace the raw source (and its credentials) inside free text such as ffmpeg's stderr."""
    shown = redact_source(source)
    out = str(text).replace(source, shown)
    if _is_url(source):
        userinfo = urlsplit(source).netloc.rpartition("@")[0]
        if userinfo:
            out = out.replace(userinfo, "<redacted>")
    return out


_VIDEO_RE = re.compile(r"Stream #\d+:\d+.*?: Video: .*?(\d{2,5})x(\d{2,5})")
_DURATION_RE = re.compile(r"Duration: (\d+):(\d{2}):(\d{2}(?:\.\d+)?)")


def probe(source: str) -> MediaInfo:
    """Read the stream layout of ``source`` (raises IngestError when it cannot be opened)."""
    if not _is_url(source) and not Path(source).exists():
        raise IngestError(f"video source not found: {redact_source(source)}")
    try:
        proc = subprocess.run(
            [ffmpeg_exe(), "-hide_banner", "-nostdin", "-i", source],
            capture_output=True,
            text=True,
            errors="replace",
            timeout=PROBE_TIMEOUT_S,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise IngestError(f"cannot probe {redact_source(source)}: {_scrub(str(exc), source)}") from None
    text = proc.stderr
    video = _VIDEO_RE.search(text)
    has_audio = bool(re.search(r"Stream #\d+:\d+.*?: Audio: ", text))
    if not video and not has_audio:
        last = text.strip().splitlines()[-1] if text.strip() else "no streams"
        raise IngestError(f"cannot open {redact_source(source)}: {_scrub(last, source)}")
    duration = None
    m = _DURATION_RE.search(text)
    if m:
        duration = int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3))
    return MediaInfo(
        width=int(video.group(1)) if video else None,
        height=int(video.group(2)) if video else None,
        has_video=bool(video),
        has_audio=has_audio,
        duration_s=duration,
    )


@contextlib.contextmanager
def _local_source(source: str) -> Iterator[str]:
    """Yield a source ffmpeg can decode.

    Finite HTTP(S) files served without byte-range support (``Accept-Ranges`` missing, ``Content-Length`` known) are
    spooled to a temporary file first: MP4 files with the ``moov`` atom at the end need seeking, which such servers
    cannot provide. Live streams (no Content-Length), HLS/DASH playlists and range-capable servers are passed to
    ffmpeg unchanged.
    """
    if not re.match(r"^https?://", source, re.IGNORECASE):
        yield source
        return
    import httpx

    try:
        with httpx.Client(follow_redirects=True, timeout=30.0) as client, client.stream("GET", source) as resp:
            if resp.status_code >= 400:
                raise IngestError(f"cannot open {redact_source(source)}: HTTP {resp.status_code}")
            ranges = resp.headers.get("accept-ranges", "").lower() == "bytes"
            length = resp.headers.get("content-length")
            ctype = resp.headers.get("content-type", "").lower()
            playlist = any(t in ctype for t in ("mpegurl", "dash+xml")) or re.search(
                r"\.(m3u8|mpd)(\?|$)", source, re.IGNORECASE
            )
            if ranges or length is None or playlist:
                spooled = None
            else:
                if int(length) > MAX_SPOOL_BYTES:
                    raise IngestError(
                        f"{redact_source(source)} is too large to spool ({length} bytes) and has no range support"
                    )
                fd, spooled = tempfile.mkstemp(prefix="perception-", suffix=Path(source.split("?")[0]).suffix)
                with os.fdopen(fd, "wb") as fh:
                    for block in resp.iter_bytes():
                        fh.write(block)
                logger.info(
                    "spooled http source without range support",
                    extra={"source": redact_source(source), "bytes": int(length)},
                )
    except httpx.HTTPError as exc:
        raise IngestError(f"cannot open {redact_source(source)}: {_scrub(str(exc), source)}") from None
    try:
        yield spooled or source
    finally:
        if spooled:
            with contextlib.suppress(OSError):
                os.unlink(spooled)


def _passes(loop: bool, max_loops: int | None) -> Iterator[int]:
    n = 0
    while True:
        if n > 0 and not loop:
            return
        if loop and max_loops is not None and n >= max_loops:
            return
        yield n
        n += 1


def _read_exact(stream, size: int) -> bytes:
    chunks, remaining = [], size
    while remaining > 0:
        data = stream.read(remaining)
        if not data:
            break
        chunks.append(data)
        remaining -= len(data)
    return b"".join(chunks)


def _spawn(args: list[str]) -> subprocess.Popen:
    try:
        return subprocess.Popen(
            [ffmpeg_exe(), "-hide_banner", "-nostdin", "-loglevel", "error", *args],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            bufsize=0,
        )
    except OSError as exc:
        raise IngestError(f"cannot start ffmpeg: {exc}") from exc


def _stop(proc: subprocess.Popen) -> None:
    if proc.poll() is None:
        proc.kill()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:  # pragma: no cover - defensive
        pass
    if proc.stdout:
        proc.stdout.close()


def iter_keyframes(
    source: str, *, start_time: datetime, fps: float = 1.0, loop: bool = False, max_loops: int | None = None
) -> Iterator[Keyframe]:
    """Yield RGB keyframes at ``fps``. Raises IngestError when the source cannot be opened."""
    if fps <= 0:
        raise ValueError("fps must be > 0")
    with _local_source(source) as local:
        yield from _keyframes(local, start_time=start_time, fps=fps, loop=loop, max_loops=max_loops)


def _keyframes(
    source: str, *, start_time: datetime, fps: float, loop: bool, max_loops: int | None
) -> Iterator[Keyframe]:
    info = probe(source)
    if not info.has_video or not info.width or not info.height:
        raise IngestError(f"{redact_source(source)} has no video stream")
    frame_bytes = info.width * info.height * 3
    index = 0
    last_pts = -1.0
    offset = 0.0
    for n_pass in _passes(loop, max_loops):
        proc = _spawn(
            ["-noautorotate", "-i", source, "-an", "-vf", f"fps={fps}", "-f", "rawvideo", "-pix_fmt", "rgb24", "pipe:1"]
        )
        produced = 0
        try:
            while True:
                data = _read_exact(proc.stdout, frame_bytes)
                if len(data) < frame_bytes:
                    break
                pts = offset + produced / fps
                produced += 1
                if pts <= last_pts:  # never emit a non-increasing timestamp (pass boundary rounding)
                    continue
                image = np.frombuffer(data, dtype=np.uint8).reshape(info.height, info.width, 3).copy()
                last_pts = pts
                yield Keyframe(timestamp=start_time + timedelta(seconds=pts), image=image, pts=pts, index=index)
                index += 1
        finally:
            _stop(proc)
        if produced == 0:
            if n_pass == 0:
                raise IngestError(f"no frames decoded from {redact_source(source)}")
            return
        offset += info.duration_s if info.duration_s else produced / fps
        offset = max(offset, last_pts + 1.0 / fps)
        logger.debug(
            "ingest video pass done", extra={"source": redact_source(source), "pass": n_pass, "frames": produced}
        )


def iter_audio_chunks(
    source: str,
    *,
    start_time: datetime,
    chunk_s: float = 5.0,
    sample_rate: int = 16000,
    loop: bool = False,
    max_loops: int | None = None,
) -> Iterator[AudioChunk]:
    """Yield mono int16 audio chunks of ``chunk_s`` seconds (the last one may be shorter). A source without an
    audio stream yields nothing."""
    if chunk_s <= 0:
        raise ValueError("chunk_s must be > 0")
    with _local_source(source) as local:
        yield from _audio_chunks(
            local, start_time=start_time, chunk_s=chunk_s, sample_rate=sample_rate, loop=loop, max_loops=max_loops
        )


def _audio_chunks(
    source: str, *, start_time: datetime, chunk_s: float, sample_rate: int, loop: bool, max_loops: int | None
) -> Iterator[AudioChunk]:
    info = probe(source)
    if not info.has_audio:
        return
    chunk_samples = max(1, round(chunk_s * sample_rate))
    min_tail = int(MIN_TAIL_S * sample_rate)
    offset = 0.0
    for _n_pass in _passes(loop, max_loops):
        args = ["-i", source, "-vn", "-ac", "1", "-ar", str(sample_rate)]
        if info.duration_s:
            args += ["-t", f"{info.duration_s:.6f}"]
        proc = _spawn([*args, "-f", "s16le", "pipe:1"])
        produced = 0
        try:
            while True:
                data = _read_exact(proc.stdout, chunk_samples * 2)
                n = len(data) // 2
                if n == 0 or (n < chunk_samples and n < min_tail):
                    break
                samples = np.frombuffer(data[: n * 2], dtype="<i2").astype(np.int16)
                ts = start_time + timedelta(seconds=offset + produced / sample_rate)
                produced += n
                yield AudioChunk(timestamp=ts, samples=samples, sample_rate=sample_rate)
                if n < chunk_samples:
                    break
        finally:
            _stop(proc)
        if produced == 0:
            return
        offset += info.duration_s if info.duration_s else produced / sample_rate


def ingest(
    source: str,
    *,
    start_time: datetime,
    fps: float = 1.0,
    chunk_s: float = 5.0,
    sample_rate: int = 16000,
    loop: bool = False,
    max_loops: int | None = None,
) -> Iterator[Keyframe | AudioChunk]:
    """Keyframes and audio chunks merged in non-decreasing timestamp order (audio first on ties)."""
    frames = iter_keyframes(source, start_time=start_time, fps=fps, loop=loop, max_loops=max_loops)
    audio = iter_audio_chunks(
        source, start_time=start_time, chunk_s=chunk_s, sample_rate=sample_rate, loop=loop, max_loops=max_loops
    )
    keyed_audio = ((c.timestamp, 0, c) for c in audio)
    keyed_frames = ((f.timestamp, 1, f) for f in frames)
    for _ts, _order, item in heapq.merge(keyed_audio, keyed_frames, key=lambda t: (t[0], t[1])):
        yield item
