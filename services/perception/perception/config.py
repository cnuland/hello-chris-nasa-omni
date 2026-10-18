"""Configuration (environment variables only; no secrets in code).

Environment variables (all optional unless noted):
  INVENTORY_API_URL, OMNI_URL, OMNI_MODEL, OMNI_REALTIME (1/true -> realtime client), DECISION_URL, DECISION_MODEL,
  ASR_URL, ASR_MODEL, S3_ENDPOINT, S3_BUCKET, S3_ACCESS_KEY, S3_SECRET_KEY, S3_REGION,
  CAMERA_LOCATIONS ("cam-a=LAB1,cam-b=NOD2" or JSON object),
  CAMERA_ZONES (JSON {"cam-a": {"LAB1S2": [x1, y1, x2, y2], ...}}: camera-configured zones, normalised boxes),
  PERCEPTION_FPS, PERCEPTION_AUDIO_CHUNK_S, PERCEPTION_WINDOW_FRAMES, PERCEPTION_STRIDE_S, PERCEPTION_LONG_SIDE,
  PERCEPTION_MIN_SCORE, PERCEPTION_LABEL_FLOOR, PERCEPTION_MIN_TRANSIT_S, PERCEPTION_VAD_RMS,
  PERCEPTION_CATALOG_REFRESH_S, PERCEPTION_EVENT_DEDUP_S,
  PERCEPTION_DETECT_LONG_SIDE (AC-PER-21, default 1280, 0 = detect on the omni image, max 4096),
  PERCEPTION_MIN_CROP_PX (AC-PER-22, default 32), PERCEPTION_NO_EXEMPLAR_MATCH_S (AC-PER-22, default 600),
  PERCEPTION_CONSOLIDATE_IOU (AC-PER-19, default 0.5, <= 1),
  PERCEPTION_HINT_CONFLICT_CONFIDENCE (AC-PER-23, default 0.5, <= 1),
  PERCEPTION_PERSON_MATCH_FLOOR (AC-PER-35, default 0.5, <= 1),
  PERCEPTION_PRIVACY_HOLD_S (AC-PER-37, default 30, 0 = off),
  PERCEPTION_MODULE_CARRY_S (AC-PER-41, default 45, 0 = off, max 600),
  PERCEPTION_MODULE_CARRY_CONFIDENCE (AC-PER-41, default 0.5, <= 1),
  OMNI_MAX_TOKENS (AC-PER-25, default 700, 1..32768), OMNI_MAX_OBJECTS (AC-PER-25/27, default 12, 1..40),
  OMNI_REPETITION_PENALTY (AC-PER-25, default 1.05, > 0 and <= 2; not sent when 1.0),
  OMNI_TRUNCATED_CONFIDENCE (AC-PER-26, default 0.5, <= 1).

A camera that has no entry in the camera -> location map is treated as no_capture (fail closed), and the CLI refuses
to start for it.
"""

from __future__ import annotations

import json
import math
import os
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any


class ConfigError(ValueError):
    """Invalid configuration value."""


DEFAULT_CAMERA_LOCATIONS = {"cam-lab1-fwd": "LAB1", "cam-nod2-aft": "NOD2", "cam-handheld": "USOS"}


@dataclass
class PerceptionConfig:
    # endpoints
    inventory_api_url: str | None = None
    omni_url: str | None = None
    omni_model: str = "Qwen/Qwen2.5-Omni-7B"
    omni_realtime: bool = False
    # AC-PER-24..27 omni output budget and truncation salvage
    omni_max_tokens: int = 700
    omni_max_objects: int = 12
    omni_repetition_penalty: float = 1.05
    omni_truncated_confidence: float = 0.5  # sighting confidence cap for a salvaged (truncated) window
    decision_url: str | None = None
    decision_model: str = "google/diffusiongemma-26B-A4B-it"
    asr_url: str | None = None
    asr_model: str = "Qwen/Qwen3-ASR-1.7B"
    s3_endpoint: str | None = None
    s3_bucket: str = "inventory"
    s3_access_key: str | None = None
    s3_secret_key: str | None = None
    s3_region: str = "us-east-1"
    camera_locations: dict[str, str] = field(default_factory=lambda: dict(DEFAULT_CAMERA_LOCATIONS))
    camera_zones: dict[str, dict[str, tuple[float, float, float, float]]] = field(default_factory=dict)
    # AC-PER-1 ingest
    fps: float = 1.0
    audio_chunk_s: float = 5.0
    audio_sample_rate: int = 16000
    # AC-PER-2 windows
    window_frames: int = 6
    stride_s: float = 4.0
    long_side: int = 448
    # AC-PER-21 detection / crop resolution: original frame capped at this long side (0 = use the omni image)
    detect_long_side: int = 1280
    # AC-PER-5 detector
    min_score: float = 0.15
    # AC-PER-5/6 torch device for OWLv2 + DINOv2: cpu | cuda | cuda:N | auto (PERCEPTION_DEVICE)
    device: str = "cpu"
    # AC-PER-7 label mapping
    label_floor: float = 0.55
    # AC-PER-8 reconciler
    link_iou: float = 0.3
    consolidate_iou: float = 0.5  # AC-PER-19 same-class omni objects of one window with IoU >= this are one
    min_crop_px: int = 32  # AC-PER-22 no exemplar when the crop's short side is below this (hires pixels)
    no_exemplar_match_s: float = 600.0  # AC-PER-22 class-and-place match horizon for sightings without exemplar
    hint_conflict_confidence: float = 0.5  # AC-PER-23 sighting confidence cap when a fixed camera's hint conflicts
    person_match_floor: float = 0.5  # AC-PER-35 appearance-text similarity needed to attach a person sighting
    person_cross_module_s: float = 600.0  # AC-PER-45 a person track last seen anywhere this recently is a candidate
    person_soft_cap: bool = True  # AC-PER-47 at the person class's expected_count, prefer attaching (floor-bounded)
    privacy_hold_s: float = 30.0  # AC-PER-37 windows of the camera starting this soon after a private one stay private
    module_carry_s: float = 45.0  # AC-PER-41 a roaming camera's unresolved window inherits the module this long
    module_carry_confidence: float = 0.5  # AC-PER-41 sighting confidence cap in a carried window
    min_transit_s: float = 20.0
    assign_p: float = 0.85
    ask_p: float = 0.5
    decision_accept: float = 0.6
    candidates_k: int = 10
    unknown_candidates_k: int = 50  # UNKNOWN has no class filter: look further so look-alikes cannot crowd it out
    catalog_refresh_s: float = 300.0  # reload catalog + locations (review resolutions add classes); 0 = never
    # AC-PER-10 events
    event_dedup_s: float = 10.0  # same (entity, type, destination) within this many seconds = one event
    # AC-PER-12 decision client
    decision_timeout_s: float = 2.0
    # AC-PER-16 speech
    speech_max_confidence: float = 0.6
    speech_confirm_s: float = 600.0
    # AC-PER-17 silence
    vad_rms: float = 300.0
    hallucination_repeats: int = 3

    @classmethod
    def from_env(cls, environ: dict[str, str] | None = None) -> PerceptionConfig:
        """Build the config from the environment (ConfigError on malformed numbers)."""
        env = dict(os.environ if environ is None else environ)
        kwargs: dict[str, Any] = {}
        for name, var in _STRING_VARS.items():
            value = env.get(var)
            if value is not None and value.strip():
                kwargs[name] = value.strip()
        for name, (var, kind) in _NUMBER_VARS.items():
            value = env.get(var)
            if value is not None and value.strip():
                kwargs[name] = _parse_number(var, value, kind)
                if name in _MAX_VALUES and kwargs[name] > _MAX_VALUES[name]:
                    raise ConfigError(f"{var} must be <= {_MAX_VALUES[name]:g}, got {value!r}")
                if name in _POSITIVE and kwargs[name] <= 0:
                    raise ConfigError(f"{var} must be > 0, got {value!r}")
        if env.get("PERCEPTION_DEVICE", "").strip():
            kwargs["device"] = _parse_device(env["PERCEPTION_DEVICE"])
        if env.get("OMNI_REALTIME", "").strip():
            kwargs["omni_realtime"] = _parse_bool("OMNI_REALTIME", env["OMNI_REALTIME"])
        if env.get("PERCEPTION_PERSON_SOFT_CAP", "").strip():
            kwargs["person_soft_cap"] = _parse_bool("PERCEPTION_PERSON_SOFT_CAP", env["PERCEPTION_PERSON_SOFT_CAP"])
        cameras = dict(DEFAULT_CAMERA_LOCATIONS)
        if env.get("CAMERA_LOCATIONS", "").strip():
            cameras.update(parse_camera_locations(env["CAMERA_LOCATIONS"]))
        kwargs["camera_locations"] = cameras
        if env.get("CAMERA_ZONES", "").strip():
            kwargs["camera_zones"] = parse_camera_zones(env["CAMERA_ZONES"])
        return cls(**kwargs)


_STRING_VARS = {
    "inventory_api_url": "INVENTORY_API_URL",
    "omni_url": "OMNI_URL",
    "omni_model": "OMNI_MODEL",
    "decision_url": "DECISION_URL",
    "decision_model": "DECISION_MODEL",
    "asr_url": "ASR_URL",
    "asr_model": "ASR_MODEL",
    "s3_endpoint": "S3_ENDPOINT",
    "s3_bucket": "S3_BUCKET",
    "s3_access_key": "S3_ACCESS_KEY",
    "s3_secret_key": "S3_SECRET_KEY",
    "s3_region": "S3_REGION",
}

_NUMBER_VARS: dict[str, tuple[str, type]] = {
    "fps": ("PERCEPTION_FPS", float),
    "audio_chunk_s": ("PERCEPTION_AUDIO_CHUNK_S", float),
    "window_frames": ("PERCEPTION_WINDOW_FRAMES", int),
    "stride_s": ("PERCEPTION_STRIDE_S", float),
    "long_side": ("PERCEPTION_LONG_SIDE", int),
    "min_score": ("PERCEPTION_MIN_SCORE", float),
    "label_floor": ("PERCEPTION_LABEL_FLOOR", float),
    "min_transit_s": ("PERCEPTION_MIN_TRANSIT_S", float),
    "vad_rms": ("PERCEPTION_VAD_RMS", float),
    "catalog_refresh_s": ("PERCEPTION_CATALOG_REFRESH_S", float),
    "event_dedup_s": ("PERCEPTION_EVENT_DEDUP_S", float),
    "decision_timeout_s": ("DECISION_TIMEOUT_S", float),
    "detect_long_side": ("PERCEPTION_DETECT_LONG_SIDE", int),
    "min_crop_px": ("PERCEPTION_MIN_CROP_PX", int),
    "no_exemplar_match_s": ("PERCEPTION_NO_EXEMPLAR_MATCH_S", float),
    "consolidate_iou": ("PERCEPTION_CONSOLIDATE_IOU", float),
    "hint_conflict_confidence": ("PERCEPTION_HINT_CONFLICT_CONFIDENCE", float),
    "person_match_floor": ("PERCEPTION_PERSON_MATCH_FLOOR", float),
    "person_cross_module_s": ("PERCEPTION_PERSON_CROSS_MODULE_S", float),
    "privacy_hold_s": ("PERCEPTION_PRIVACY_HOLD_S", float),
    "module_carry_s": ("PERCEPTION_MODULE_CARRY_S", float),
    "module_carry_confidence": ("PERCEPTION_MODULE_CARRY_CONFIDENCE", float),
    "omni_max_tokens": ("OMNI_MAX_TOKENS", int),
    "omni_max_objects": ("OMNI_MAX_OBJECTS", int),
    "omni_repetition_penalty": ("OMNI_REPETITION_PENALTY", float),
    "omni_truncated_confidence": ("OMNI_TRUNCATED_CONFIDENCE", float),
}

# Upper bounds checked by from_env (inclusive).
MAX_DETECT_LONG_SIDE = 4096  # bounds hires frame memory: <= window_frames * 4096**2 * 3 bytes
_MAX_VALUES: dict[str, float] = {
    "detect_long_side": MAX_DETECT_LONG_SIDE,
    "consolidate_iou": 1.0,
    "hint_conflict_confidence": 1.0,
    "person_match_floor": 1.0,
    "module_carry_confidence": 1.0,
    "module_carry_s": 600.0,  # AC-PER-41: a carry is short-range evidence, never an unbounded default
    "omni_max_tokens": 32768,
    "omni_max_objects": 40,  # observation.schema.json objects.maxItems
    "omni_repetition_penalty": 2.0,
    "omni_truncated_confidence": 1.0,
}
# Must be strictly positive (checked by from_env).
_POSITIVE = {"omni_max_tokens", "omni_max_objects", "omni_repetition_penalty"}

_DEVICE_RE = re.compile(r"^(cpu|auto|cuda(:[0-9]+)?)$")


def _parse_device(raw: str) -> str:
    value = raw.strip().lower()
    if not _DEVICE_RE.match(value):
        raise ConfigError(f"PERCEPTION_DEVICE must be cpu, cuda, cuda:N or auto, got {raw!r}")
    return value


def resolve_device(device: str, *, cuda_available: Callable[[], bool] | None = None) -> str:
    """``auto`` -> ``cuda`` when torch sees a GPU, else ``cpu``; explicit values are returned unchanged."""
    if device != "auto":
        return device
    if cuda_available is None:
        def cuda_available() -> bool:  # lazy: importing perception must not import torch (AC-PER-5)
            import torch

            return bool(torch.cuda.is_available())
    return "cuda" if cuda_available() else "cpu"


_TRUE = {"1", "true", "yes", "on", "y"}
_FALSE = {"0", "false", "no", "off", "n", ""}


def _parse_number(var: str, raw: str, kind: type) -> float | int:
    text = raw.strip()
    try:
        value = int(text) if kind is int else float(text)
    except ValueError as exc:
        raise ConfigError(f"{var} must be a number, got {raw!r}") from exc
    if math.isnan(value) or value < 0:
        raise ConfigError(f"{var} must be a non-negative number, got {raw!r}")
    return value


def _parse_bool(var: str, raw: str) -> bool:
    text = raw.strip().lower()
    if text in _TRUE:
        return True
    if text in _FALSE:
        return False
    raise ConfigError(f"{var} must be a boolean (1/0, true/false, yes/no), got {raw!r}")


def parse_camera_locations(raw: str) -> dict[str, str]:
    """``"cam-a=LAB1,cam-b=NOD2"`` or a JSON object -> {camera_id: location_id}."""
    text = raw.strip()
    if text.startswith("{"):
        try:
            data = json.loads(text)
        except json.JSONDecodeError as exc:
            raise ConfigError(f"CAMERA_LOCATIONS is not valid JSON: {exc}") from exc
        if not isinstance(data, dict) or not all(isinstance(v, str) for v in data.values()):
            raise ConfigError("CAMERA_LOCATIONS JSON must map camera ids to location ids")
        return {str(k): v for k, v in data.items()}
    out: dict[str, str] = {}
    for part in text.split(","):
        part = part.strip()
        if not part:
            continue
        cam, sep, loc = part.partition("=")
        if not sep or not cam.strip() or not loc.strip():
            raise ConfigError(f"CAMERA_LOCATIONS entry {part!r} must look like camera=LOCATION")
        out[cam.strip()] = loc.strip()
    return out


Box = tuple[float, float, float, float]


def parse_camera_zones(raw: str) -> dict[str, dict[str, Box]]:
    """``CAMERA_ZONES`` JSON ``{"camera_id": {"ZONE_ID": [x1, y1, x2, y2], ...}, ...}`` (normalised 0..1,
    x1 <= x2, y1 <= y2) -> {camera_id: {zone_id: box}}. ConfigError on anything else."""
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ConfigError(f"CAMERA_ZONES is not valid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise ConfigError("CAMERA_ZONES must be a JSON object {camera_id: {zone_id: [x1, y1, x2, y2]}}")
    out: dict[str, dict[str, Box]] = {}
    for camera, zones in data.items():
        if not isinstance(zones, dict):
            raise ConfigError(f"CAMERA_ZONES[{camera!r}] must be an object of zone_id -> [x1, y1, x2, y2]")
        parsed: dict[str, Box] = {}
        for zone, box in zones.items():
            if (
                not isinstance(box, list | tuple)
                or len(box) != 4
                or not all(isinstance(v, int | float) and not isinstance(v, bool) for v in box)
            ):
                raise ConfigError(f"CAMERA_ZONES[{camera!r}][{zone!r}] must be 4 numbers [x1, y1, x2, y2]")
            x1, y1, x2, y2 = (float(v) for v in box)
            if not all(0.0 <= v <= 1.0 for v in (x1, y1, x2, y2)) or x1 > x2 or y1 > y2:
                raise ConfigError(
                    f"CAMERA_ZONES[{camera!r}][{zone!r}] must be normalised 0..1 with x1 <= x2 and y1 <= y2"
                )
            parsed[str(zone)] = (x1, y1, x2, y2)
        out[str(camera)] = parsed
    return out
