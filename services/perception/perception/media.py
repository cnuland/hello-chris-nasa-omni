"""Small media helpers: JPEG encoding, downscaling, WAV packing, PCM16 resampling, RMS."""

from __future__ import annotations

import io
import math
import wave

import numpy as np
from PIL import Image


def downscale(image: np.ndarray, long_side: int) -> np.ndarray:
    """Return ``image`` resized so its longer side is ``long_side`` (aspect kept). Never upscales."""
    h, w = image.shape[:2]
    longest = max(h, w)
    if longest <= long_side or long_side <= 0:
        return image
    scale = long_side / float(longest)
    new_w = max(1, round(w * scale))
    new_h = max(1, round(h * scale))
    resized = Image.fromarray(np.ascontiguousarray(image)).resize((new_w, new_h), Image.Resampling.BILINEAR)
    return np.asarray(resized, dtype=np.uint8)


def encode_jpeg(image: np.ndarray, quality: int = 85) -> bytes:
    """Encode an HxWx3 uint8 RGB array as JPEG bytes."""
    buf = io.BytesIO()
    Image.fromarray(np.ascontiguousarray(image, dtype=np.uint8)).convert("RGB").save(
        buf, format="JPEG", quality=quality
    )
    return buf.getvalue()


def crop(image: np.ndarray, bbox: tuple[float, float, float, float] | list[float]) -> np.ndarray:
    """Crop with a normalised (x1, y1, x2, y2) bbox; clamps to the frame; never returns an empty array."""
    h, w = image.shape[:2]
    x1, y1, x2, y2 = (float(v) for v in bbox)
    if x2 < x1:
        x1, x2 = x2, x1
    if y2 < y1:
        y1, y2 = y2, y1
    px1 = min(max(math.floor(x1 * w), 0), w - 1)
    py1 = min(max(math.floor(y1 * h), 0), h - 1)
    px2 = min(max(math.ceil(x2 * w), px1 + 1), w)
    py2 = min(max(math.ceil(y2 * h), py1 + 1), h)
    return image[py1:py2, px1:px2]


def to_wav_bytes(samples: np.ndarray, sample_rate: int) -> bytes:
    """Pack mono int16 samples into a RIFF/WAVE byte string."""
    pcm = np.asarray(samples, dtype="<i2")
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(int(sample_rate))
        wf.writeframes(pcm.tobytes())
    return buf.getvalue()


def resample_pcm16(samples: np.ndarray, from_rate: int, to_rate: int) -> np.ndarray:
    """Resample mono int16 audio with linear interpolation; returns int16."""
    src = np.asarray(samples, dtype=np.float64)
    if from_rate == to_rate or src.size == 0:
        return np.asarray(samples, dtype=np.int16).copy()
    n_out = round(src.size * to_rate / float(from_rate))
    if n_out <= 0:
        return np.zeros(0, dtype=np.int16)
    x_old = np.arange(src.size) / float(from_rate)
    x_new = np.arange(n_out) / float(to_rate)
    out = np.interp(x_new, x_old, src)
    return np.clip(np.round(out), -32768, 32767).astype(np.int16)


def to_pcm16(samples: np.ndarray) -> np.ndarray:
    """Coerce a 1-D audio array to int16: float input is assumed to be in [-1, 1]."""
    arr = np.asarray(samples)
    if arr.ndim > 1:
        arr = arr.mean(axis=1) if arr.shape[1] <= 8 else arr.reshape(-1)
    if np.issubdtype(arr.dtype, np.floating):
        return np.clip(np.round(arr * 32767.0), -32768, 32767).astype(np.int16)
    if arr.dtype == np.int16:
        return arr
    return np.clip(arr, -32768, 32767).astype(np.int16)


def rms(samples: np.ndarray) -> float:
    """Root-mean-square amplitude of int16 samples (0.0 for an empty array)."""
    arr = np.asarray(samples, dtype=np.float64)
    if arr.size == 0:
        return 0.0
    return float(np.sqrt(np.mean(arr * arr)))
