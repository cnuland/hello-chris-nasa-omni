"""Deterministic synthetic scene generator (AC-EVAL-2) and decision pair generator (AC-EVAL-7).

Crops are procedurally drawn shapes/icons per catalog class (no copyrighted images), composited onto an ISS-like
background. Same seed => identical output (bytes), independent of process / PYTHONHASHSEED: all randomness comes from
``numpy.random.default_rng`` seeded with integers derived from the seed and stable class indices (never ``hash()``).

Scenario (every scene, positions / classes / timings vary with the seed):
- a tool that *moves* from one rack zone to another (``moved`` event with an actor, plus a scripted line);
- identical *twins* (same class, same crop) visible together in two zones;
- an object that *appears* part-way through;
- an object that *disappears* (``removed`` event);
- exactly one *unknown* object (catalog_id null).
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np
import yaml
from PIL import Image, ImageDraw

from nasa_eval.dataset import Appearance, GoldEvent, GoldObject, TranscriptLine, gold_to_dict

log = logging.getLogger(__name__)

SUPPORTED_CLASSES: tuple[str, ...] = (
    "laptop",
    "drill-pistol",
    "wrench",
    "scissors",
    "duct-tape",
    "flashlight",
    "multimeter",
    "camera-dslr",
    "headset",
    "tablet",
)  # catalog ids that draw_crop can render (plus "unknown")
UNKNOWN = "unknown"

# Destiny (LAB1) rack zones seen by cam-lab1-fwd, as normalised [x1, y1, x2, y2] regions of the frame.
ZONES: dict[str, tuple[float, float, float, float]] = {
    "LAB1S2": (0.03, 0.10, 0.32, 0.90),
    "LAB1O3": (0.355, 0.10, 0.645, 0.90),
    "LAB1P3": (0.68, 0.10, 0.97, 0.90),
}
ZONE_NAMES = {"LAB1S2": "the starboard rack", "LAB1O3": "the overhead rack", "LAB1P3": "the port rack"}
OBJECT_SIZE = (0.10, 0.16)  # normalised crop width / height
SLOT_GRID = (2, 3)  # columns x rows of object slots per zone
CREW = ("crew member, blue shirt", "crew member, red shirt", "crew member, green shirt")

# Scenario roles: candidate classes per role (twins are always identical crops of one class).
MOVER_CLASSES = ("drill-pistol", "multimeter", "flashlight", "camera-dslr")
TWIN_CLASSES = ("wrench", "scissors", "duct-tape")
APPEAR_CLASSES = ("tablet", "headset", "flashlight", "camera-dslr")
REMOVED_CLASSES = ("laptop", "tablet", "multimeter")


@dataclass(frozen=True)
class SceneConfig:
    duration_s: float = 60.0
    fps: float = 1.0
    width: int = 640
    height: int = 360
    camera: str = "cam-lab1-fwd"
    start_time: datetime = datetime(2026, 10, 1, 12, 0, 0, tzinfo=UTC)


@dataclass
class Scene:
    seed: int
    config: SceneConfig
    frames: list[tuple[float, Any]] = field(default_factory=list)  # (t seconds, ndarray HxWx3 uint8)
    gold: list[GoldObject] = field(default_factory=list)
    transcript: list[TranscriptLine] = field(default_factory=list)
    zone_regions: dict[str, tuple[float, float, float, float]] = field(default_factory=dict)  # loc -> normalised rect
    background: Any = None  # ndarray HxWx3 uint8


# ------------------------------------------------------------------------------------------------------- crops
Box = tuple[float, float, float, float]


def _rgb(base: tuple[int, int, int], rng: np.random.Generator, spread: int = 18) -> tuple[int, int, int]:
    jitter = rng.integers(-spread, spread + 1, size=3)
    return tuple(int(np.clip(b + j, 0, 255)) for b, j in zip(base, jitter, strict=True))  # type: ignore[return-value]


def _draw_laptop(d: ImageDraw.ImageDraw, w: int, h: int, rng: np.random.Generator) -> None:
    d.rectangle((0.12 * w, 0.10 * h, 0.88 * w, 0.58 * h), fill=_rgb((25, 25, 30), rng))
    d.rectangle((0.17 * w, 0.15 * h, 0.83 * w, 0.53 * h), fill=_rgb((70, 140, 220), rng))
    d.polygon(
        [(0.05 * w, 0.88 * h), (0.95 * w, 0.88 * h), (0.85 * w, 0.60 * h), (0.15 * w, 0.60 * h)],
        fill=_rgb((40, 40, 45), rng),
    )
    for k in range(4):
        y = (0.65 + 0.05 * k) * h
        d.line((0.22 * w, y, 0.78 * w, y), fill=_rgb((120, 120, 125), rng), width=1)


def _draw_drill(d: ImageDraw.ImageDraw, w: int, h: int, rng: np.random.Generator) -> None:
    body = _rgb((235, 190, 30), rng)
    d.rectangle((0.15 * w, 0.18 * h, 0.80 * w, 0.42 * h), fill=body)
    d.rectangle((0.80 * w, 0.26 * h, 0.95 * w, 0.34 * h), fill=_rgb((160, 160, 165), rng))
    d.polygon(
        [(0.35 * w, 0.42 * h), (0.55 * w, 0.42 * h), (0.50 * w, 0.85 * h), (0.30 * w, 0.85 * h)],
        fill=_rgb((30, 30, 30), rng),
    )
    d.rectangle((0.25 * w, 0.82 * h, 0.58 * w, 0.95 * h), fill=body)


def _draw_wrench(d: ImageDraw.ImageDraw, w: int, h: int, rng: np.random.Generator) -> None:
    steel = _rgb((190, 195, 200), rng)
    d.line((0.22 * w, 0.78 * h, 0.78 * w, 0.22 * h), fill=steel, width=max(2, int(0.12 * min(w, h))))
    r = 0.15 * min(w, h)
    for cx, cy in ((0.20 * w, 0.80 * h), (0.80 * w, 0.20 * h)):
        d.ellipse((cx - r, cy - r, cx + r, cy + r), fill=steel)
        d.ellipse((cx - r / 2, cy - r / 2, cx + r / 2, cy + r / 2), fill=_rgb((60, 70, 90), rng))


def _draw_scissors(d: ImageDraw.ImageDraw, w: int, h: int, rng: np.random.Generator) -> None:
    handle = _rgb((220, 50, 50), rng)
    blade = _rgb((210, 210, 215), rng)
    d.line((0.45 * w, 0.55 * h, 0.90 * w, 0.10 * h), fill=blade, width=max(2, w // 14))
    d.line((0.55 * w, 0.55 * h, 0.10 * w, 0.10 * h), fill=blade, width=max(2, w // 14))
    d.ellipse((0.15 * w, 0.55 * h, 0.45 * w, 0.92 * h), outline=handle, width=max(2, w // 12))
    d.ellipse((0.55 * w, 0.55 * h, 0.85 * w, 0.92 * h), outline=handle, width=max(2, w // 12))


def _draw_duct_tape(d: ImageDraw.ImageDraw, w: int, h: int, rng: np.random.Generator) -> None:
    d.ellipse((0.12 * w, 0.12 * h, 0.88 * w, 0.88 * h), fill=_rgb((150, 150, 155), rng))
    d.ellipse((0.32 * w, 0.32 * h, 0.68 * w, 0.68 * h), fill=_rgb((210, 180, 120), rng))
    d.ellipse((0.40 * w, 0.40 * h, 0.60 * w, 0.60 * h), fill=_rgb((20, 30, 50), rng))


def _draw_flashlight(d: ImageDraw.ImageDraw, w: int, h: int, rng: np.random.Generator) -> None:
    d.rectangle((0.10 * w, 0.40 * h, 0.65 * w, 0.60 * h), fill=_rgb((20, 20, 20), rng))
    d.rectangle((0.65 * w, 0.30 * h, 0.80 * w, 0.70 * h), fill=_rgb((100, 100, 110), rng))
    d.polygon(
        [(0.80 * w, 0.30 * h), (0.98 * w, 0.10 * h), (0.98 * w, 0.90 * h), (0.80 * w, 0.70 * h)],
        fill=_rgb((255, 240, 150), rng),
    )


def _draw_multimeter(d: ImageDraw.ImageDraw, w: int, h: int, rng: np.random.Generator) -> None:
    d.rectangle((0.20 * w, 0.05 * h, 0.80 * w, 0.95 * h), fill=_rgb((240, 200, 20), rng))
    d.rectangle((0.28 * w, 0.12 * h, 0.72 * w, 0.35 * h), fill=_rgb((150, 170, 140), rng))
    d.ellipse((0.35 * w, 0.45 * h, 0.65 * w, 0.75 * h), fill=_rgb((40, 40, 40), rng))
    d.ellipse((0.30 * w, 0.82 * h, 0.40 * w, 0.92 * h), fill=_rgb((200, 30, 30), rng))
    d.ellipse((0.60 * w, 0.82 * h, 0.70 * w, 0.92 * h), fill=_rgb((10, 10, 10), rng))


def _draw_camera(d: ImageDraw.ImageDraw, w: int, h: int, rng: np.random.Generator) -> None:
    d.rectangle((0.10 * w, 0.30 * h, 0.90 * w, 0.85 * h), fill=_rgb((25, 25, 25), rng))
    d.rectangle((0.35 * w, 0.18 * h, 0.60 * w, 0.30 * h), fill=_rgb((35, 35, 35), rng))
    d.ellipse((0.32 * w, 0.38 * h, 0.68 * w, 0.80 * h), fill=_rgb((80, 80, 90), rng))
    d.ellipse((0.42 * w, 0.48 * h, 0.58 * w, 0.70 * h), fill=_rgb((40, 90, 160), rng))
    d.rectangle((0.75 * w, 0.22 * h, 0.85 * w, 0.30 * h), fill=_rgb((200, 30, 30), rng))


def _draw_headset(d: ImageDraw.ImageDraw, w: int, h: int, rng: np.random.Generator) -> None:
    band = _rgb((60, 60, 70), rng)
    d.arc((0.15 * w, 0.08 * h, 0.85 * w, 0.90 * h), start=180, end=360, fill=band, width=max(2, w // 10))
    cup = _rgb((200, 200, 205), rng)
    d.ellipse((0.08 * w, 0.45 * h, 0.32 * w, 0.85 * h), fill=cup)
    d.ellipse((0.68 * w, 0.45 * h, 0.92 * w, 0.85 * h), fill=cup)
    d.line((0.20 * w, 0.80 * h, 0.50 * w, 0.95 * h), fill=_rgb((30, 30, 30), rng), width=max(1, w // 30))


def _draw_tablet(d: ImageDraw.ImageDraw, w: int, h: int, rng: np.random.Generator) -> None:
    d.rounded_rectangle(
        (0.15 * w, 0.08 * h, 0.85 * w, 0.92 * h), radius=max(1, int(0.08 * w)), fill=_rgb((210, 210, 215), rng)
    )
    d.rectangle((0.21 * w, 0.15 * h, 0.79 * w, 0.82 * h), fill=_rgb((30, 160, 120), rng))
    d.ellipse((0.47 * w, 0.84 * h, 0.53 * w, 0.90 * h), fill=_rgb((90, 90, 90), rng))


def _draw_unknown(d: ImageDraw.ImageDraw, w: int, h: int, rng: np.random.Generator) -> None:
    n = 7
    angles = np.sort(rng.uniform(0, 2 * np.pi, size=n))
    radii = rng.uniform(0.25, 0.45, size=n)
    pts = [(w * (0.5 + r * np.cos(a)), h * (0.5 + r * np.sin(a))) for a, r in zip(angles, radii, strict=True)]
    d.polygon(pts, fill=_rgb((200, 60, 200), rng, spread=40))
    for _ in range(3):
        cx, cy = rng.uniform(0.3, 0.7) * w, rng.uniform(0.3, 0.7) * h
        r = 0.06 * min(w, h)
        d.ellipse((cx - r, cy - r, cx + r, cy + r), fill=_rgb((40, 220, 220), rng, spread=30))


_DRAWERS: dict[str, Callable[[ImageDraw.ImageDraw, int, int, np.random.Generator], None]] = {
    "laptop": _draw_laptop,
    "drill-pistol": _draw_drill,
    "wrench": _draw_wrench,
    "scissors": _draw_scissors,
    "duct-tape": _draw_duct_tape,
    "flashlight": _draw_flashlight,
    "multimeter": _draw_multimeter,
    "camera-dslr": _draw_camera,
    "headset": _draw_headset,
    "tablet": _draw_tablet,
    UNKNOWN: _draw_unknown,
}
_CLASS_INDEX = {c: i for i, c in enumerate((*SUPPORTED_CLASSES, UNKNOWN))}
# Dark, per-class card colours so every crop contrasts with the light module background.
_CARD = {c: (int(25 + 9 * i), int(45 + 5 * (i % 4)), int(70 + 7 * (i % 5))) for c, i in _CLASS_INDEX.items()}


def draw_crop(class_key: str, seed: int, size: tuple[int, int] = (64, 64)) -> Any:
    """Return an RGB uint8 ndarray (h, w, 3) icon for ``class_key`` (a catalog id or "unknown").

    ``size`` is (width, height). The same (class, seed, size) always renders the same pixels; the seed varies
    colours and small details within the class."""
    if class_key not in _DRAWERS:
        raise KeyError(f"no crop renderer for class {class_key!r}; supported: {(*SUPPORTED_CLASSES, UNKNOWN)}")
    w, h = int(size[0]), int(size[1])
    if w < 4 or h < 4:
        raise ValueError(f"crop size {size} too small")
    rng = np.random.default_rng([int(seed) & 0xFFFFFFFF, _CLASS_INDEX[class_key]])
    img = Image.new("RGB", (w, h), _rgb(_CARD[class_key], rng, spread=10))
    d = ImageDraw.Draw(img)
    d.rectangle((0, 0, w - 1, h - 1), outline=_rgb((15, 15, 20), rng, spread=5))
    _DRAWERS[class_key](d, w, h, rng)
    return np.asarray(img, dtype=np.uint8).copy()


# ------------------------------------------------------------------------------------------------------- background
def _background(cfg: SceneConfig, rng: np.random.Generator) -> np.ndarray:
    w, h = cfg.width, cfg.height
    img = Image.new("RGB", (w, h), _rgb((205, 208, 212), rng, spread=6))
    d = ImageDraw.Draw(img)
    # module shell: ceiling / floor bands and handrails
    d.rectangle((0, 0, w, int(0.07 * h)), fill=_rgb((175, 178, 182), rng, spread=4))
    d.rectangle((0, int(0.93 * h), w, h), fill=_rgb((165, 168, 172), rng, spread=4))
    for x1, y1, x2, y2 in ZONES.values():
        box = (int(x1 * w), int(y1 * h), int(x2 * w) - 1, int(y2 * h) - 1)
        d.rectangle(box, fill=_rgb((222, 224, 226), rng, spread=4), outline=_rgb((150, 150, 155), rng, spread=4))
        for k in range(1, 4):  # rack drawer seams
            y = int((y1 + (y2 - y1) * k / 4) * h)
            d.line((box[0] + 2, y, box[2] - 2, y), fill=_rgb((185, 187, 190), rng, spread=4))
    d.line((0, int(0.085 * h), w, int(0.085 * h)), fill=_rgb((230, 200, 60), rng, spread=8), width=2)
    arr = np.asarray(img, dtype=np.int16)
    noise = rng.integers(-3, 4, size=arr.shape, dtype=np.int16)
    return np.clip(arr + noise, 0, 255).astype(np.uint8)


# ------------------------------------------------------------------------------------------------------- scene
@dataclass
class _Track:
    gold_object_id: str
    class_key: str
    crop_seed: int
    segments: list[tuple[int, int, str]]  # (first frame, last frame exclusive, zone)
    is_unknown: bool = False
    events: list[GoldEvent] = field(default_factory=list)


def _slot_box(zone: str, slot: int, rng: np.random.Generator) -> Box:
    zx1, zy1, zx2, zy2 = ZONES[zone]
    cols, rows = SLOT_GRID
    col, row = slot % cols, slot // cols
    cw, ch = (zx2 - zx1) / cols, (zy2 - zy1) / rows
    ow, oh = OBJECT_SIZE
    cx = zx1 + cw * (col + 0.5) + rng.uniform(-0.15, 0.15) * (cw - ow)
    cy = zy1 + ch * (row + 0.5) + rng.uniform(-0.15, 0.15) * (ch - oh)
    return (cx - ow / 2, cy - oh / 2, cx + ow / 2, cy + oh / 2)


def _pixel_box(box: Box, w: int, h: int) -> tuple[int, int, int, int]:
    return (round(box[0] * w), round(box[1] * h), round(box[2] * w), round(box[3] * h))


def _choose(rng: np.random.Generator, options: tuple[str, ...], exclude: set[str]) -> str:
    pool = [o for o in options if o not in exclude]
    return pool[int(rng.integers(len(pool)))]


def generate_scene(seed: int, config: SceneConfig | None = None) -> Scene:
    """Build a deterministic synthetic scene (frames + gold + transcript) for ``seed``."""
    cfg = config or SceneConfig()
    n = round(cfg.duration_s * cfg.fps)
    if n < 2:
        raise ValueError("a scene needs at least 2 frames (duration_s * fps >= 2)")
    rng = np.random.default_rng([int(seed) & 0xFFFFFFFF, 0x5EED])
    zones = list(ZONES)

    def frame_at(frac: float) -> int:
        return min(n - 1, max(1, round(frac * n)))

    used: set[str] = set()
    mover_cls = _choose(rng, MOVER_CLASSES, used)
    used.add(mover_cls)
    twin_cls = _choose(rng, TWIN_CLASSES, used)
    used.add(twin_cls)
    appear_cls = _choose(rng, APPEAR_CLASSES, used)
    used.add(appear_cls)
    removed_cls = _choose(rng, REMOVED_CLASSES, used)

    order = [zones[i] for i in rng.permutation(len(zones))]
    move_from, move_to, third = order
    move_f = frame_at(rng.uniform(0.35, 0.5))
    appear_f = frame_at(rng.uniform(0.2, 0.3))
    removed_f = frame_at(rng.uniform(0.4, 0.5))
    unknown_f0 = frame_at(rng.uniform(0.1, 0.2))
    unknown_f1 = max(unknown_f0 + 1, frame_at(rng.uniform(0.8, 0.9)))
    actor_move, actor_remove = (CREW[i] for i in rng.permutation(len(CREW))[:2])

    def t_of(frame: int) -> float:
        return round(frame / cfg.fps, 6)

    twin_seed = int(rng.integers(1 << 30))
    tracks = [
        _Track(
            f"{mover_cls}-1",
            mover_cls,
            int(rng.integers(1 << 30)),
            [(0, move_f, move_from), (move_f, n, move_to)],
            events=[GoldEvent("moved", t_of(move_f), actor_move, move_to)],
        ),
        _Track(f"{twin_cls}-a", twin_cls, twin_seed, [(0, n, move_from)]),
        _Track(f"{twin_cls}-b", twin_cls, twin_seed, [(0, n, third)]),
        _Track(f"{appear_cls}-1", appear_cls, int(rng.integers(1 << 30)), [(appear_f, n, move_to)]),
        _Track(
            f"{removed_cls}-1",
            removed_cls,
            int(rng.integers(1 << 30)),
            [(0, removed_f, third)],
            events=[GoldEvent("removed", t_of(removed_f), actor_remove, None)],
        ),
        _Track("unknown-1", UNKNOWN, int(rng.integers(1 << 30)), [(unknown_f0, unknown_f1, third)], is_unknown=True),
    ]

    # one non-overlapping slot per (track, zone)
    slots_left = {z: [int(s) for s in rng.permutation(SLOT_GRID[0] * SLOT_GRID[1])] for z in zones}
    boxes: dict[tuple[str, str], Box] = {}
    for tr in tracks:
        for _, _, z in tr.segments:
            if (tr.gold_object_id, z) not in boxes:
                boxes[(tr.gold_object_id, z)] = tuple(round(v, 6) for v in _slot_box(z, slots_left[z].pop(), rng))

    background = _background(cfg, rng)
    ow, oh = OBJECT_SIZE
    crop_px = (max(4, round(ow * cfg.width)), max(4, round(oh * cfg.height)))
    crops = {tr.gold_object_id: draw_crop(tr.class_key, tr.crop_seed, crop_px) for tr in tracks}

    frames: list[tuple[float, Any]] = []
    appearances: dict[str, list[Appearance]] = {tr.gold_object_id: [] for tr in tracks}
    for i in range(n):
        frame = background.copy()
        for tr in tracks:
            zone = next((z for f0, f1, z in tr.segments if f0 <= i < f1), None)
            if zone is None:
                continue
            box = boxes[(tr.gold_object_id, zone)]
            x1, y1, x2, y2 = _pixel_box(box, cfg.width, cfg.height)
            crop = crops[tr.gold_object_id]
            if crop.shape[:2] != (y2 - y1, x2 - x1):
                crop = np.asarray(Image.fromarray(crop).resize((x2 - x1, y2 - y1), Image.NEAREST))
            frame[y1:y2, x1:x2] = crop
            appearances[tr.gold_object_id].append(Appearance(t_of(i), zone, box))
        frames.append((t_of(i), frame))

    gold = [
        GoldObject(
            tr.gold_object_id,
            None if tr.is_unknown else tr.class_key,
            appearances[tr.gold_object_id],
            list(tr.events),
            tr.is_unknown,
        )
        for tr in tracks
    ]

    pretty = {
        c: c.replace("-", " ").replace("drill pistol", "pistol-grip drill").replace("camera dslr", "camera")
        for c in SUPPORTED_CLASSES
    }
    transcript = sorted(
        [
            TranscriptLine(0.0, "ground", "Destiny, ground. Starting the tool inventory survey on the lab camera."),
            TranscriptLine(
                t_of(max(0, move_f - 1)),
                actor_move,
                f"I'm moving the {pretty[mover_cls]} over to {ZONE_NAMES[move_to]}.",
            ),
            TranscriptLine(
                t_of(max(0, removed_f - 1)), actor_remove, f"Taking the {pretty[removed_cls]} out of the lab for now."
            ),
            TranscriptLine(
                t_of(unknown_f0),
                actor_move,
                f"Not sure what this thing on {ZONE_NAMES[third]} is, flagging it for the ground.",
            ),
        ],
        key=lambda x: (x.t, x.speaker, x.text),
    )
    scene = Scene(
        seed=seed,
        config=cfg,
        frames=frames,
        gold=gold,
        transcript=transcript,
        zone_regions=dict(ZONES),
        background=background,
    )
    log.debug("generated scene seed=%s frames=%d gold=%d", seed, n, len(gold))
    return scene


def write_scene(scene: Scene, out_dir: str | Path, name: str) -> Path:
    """Write ``<out_dir>/<name>/`` (manifest.yaml, gold.jsonl, transcript.jsonl, frames/*.png). Returns the dir."""
    d = Path(out_dir) / name
    frames_dir = d / "frames"
    frames_dir.mkdir(parents=True, exist_ok=True)
    for old in frames_dir.glob("*.png"):
        old.unlink()
    cfg = scene.config
    manifest = {
        "name": name,
        "description": f"synthetic ISS lab scene (nasa_eval.synth seed {scene.seed})",
        "seed": int(scene.seed),
        "video": "frames",
        "audio": None,
        "camera": cfg.camera,
        "start_time": cfg.start_time.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%f").rstrip("0").rstrip(".") + "Z",
        "fps": float(cfg.fps),
        "transcript": "transcript.jsonl",
        "zones": {k: list(v) for k, v in scene.zone_regions.items()},
    }
    (d / "manifest.yaml").write_text(yaml.safe_dump(manifest, sort_keys=False))
    (d / "gold.jsonl").write_text("".join(json.dumps(gold_to_dict(g)) + "\n" for g in scene.gold))
    (d / "transcript.jsonl").write_text(
        "".join(json.dumps({"t": x.t, "speaker": x.speaker, "text": x.text}) + "\n" for x in scene.transcript)
    )
    for i, (_, frame) in enumerate(scene.frames):
        Image.fromarray(frame, "RGB").save(frames_dir / f"{i:06d}.png", optimize=False)
    log.info("wrote scene name=%s dir=%s frames=%d", name, d, len(scene.frames))
    return d


# ------------------------------------------------------------------------------------------------------- pairs
def _augment(crop: np.ndarray, rng: np.random.Generator) -> np.ndarray:
    """A second view of the same item: brightness shift, small translation and sensor noise."""
    arr = crop.astype(np.int16) + int(rng.integers(-20, 21))
    dx, dy = (int(v) for v in rng.integers(-2, 3, size=2))
    arr = np.roll(arr, (dy, dx), axis=(0, 1))
    arr += rng.integers(-6, 7, size=arr.shape, dtype=np.int16)
    return np.clip(arr, 0, 255).astype(np.uint8)


def generate_pairs(seed: int, out_dir: str | Path, *, n_positive: int = 20, n_negative: int = 20) -> Path:
    """Write ``pairs.jsonl`` + ``crops/*.png`` into out_dir for the decision-model eval. Returns pairs.jsonl path.

    Positives are two views of the same item. Negatives alternate between hard same-class look-alikes (an identical
    twin class: same class, different instance) and different-class items. The stratum is written to the
    evaluation-only ``meta`` ({"kind", "hard_negative"}), never to ``context`` (review round: the old
    ``context.hard_negative`` / null ``catalog_id`` leaked the label). ``context`` carries what the pipeline would
    know: ``catalog_id`` = the observation's class (always set) and ``similarity`` = the embedding similarity of the
    two saved crops (``decision_eval.similarity_of`` with the deterministic CropEmbedder)."""
    from nasa_eval.decision_eval import similarity_of

    if n_positive < 0 or n_negative < 0 or n_positive + n_negative == 0:
        raise ValueError("need at least one pair")
    out = Path(out_dir)
    crops_dir = out / "crops"
    crops_dir.mkdir(parents=True, exist_ok=True)
    rng = np.random.default_rng([int(seed) & 0xFFFFFFFF, 0x9A1F])
    zones = list(ZONES)
    specs: list[tuple[str, str, str, int, str, int]] = []  # (label, kind, obs_cls, obs_seed, cand_cls, cand_seed)
    for _ in range(n_positive):
        cls = SUPPORTED_CLASSES[int(rng.integers(len(SUPPORTED_CLASSES)))]
        inst = int(rng.integers(1 << 30))
        specs.append(("same", "positive", cls, inst, cls, inst))
    for k in range(n_negative):
        cls = SUPPORTED_CLASSES[int(rng.integers(len(SUPPORTED_CLASSES)))]
        if k % 2 == 0:  # hard negative: same class, different instance
            specs.append(("new", "hard_negative", cls, int(rng.integers(1 << 30)), cls, int(rng.integers(1 << 30))))
        else:
            other = _choose(rng, (*SUPPORTED_CLASSES, UNKNOWN), {cls})
            specs.append(("new", "class_negative", cls, int(rng.integers(1 << 30)), other, int(rng.integers(1 << 30))))
    order = rng.permutation(len(specs))
    rows = []
    for idx, si in enumerate(order):
        label, kind, obs_cls, obs_seed, cand_cls, cand_seed = specs[int(si)]
        pid = f"p{idx:04d}"
        cand = draw_crop(cand_cls, cand_seed, (64, 64))
        obs = _augment(draw_crop(obs_cls, obs_seed, (64, 64)), rng)
        obs_rel, cand_rel = f"crops/{pid}-obs.png", f"crops/{pid}-cand.png"
        Image.fromarray(obs, "RGB").save(out / obs_rel)
        Image.fromarray(cand, "RGB").save(out / cand_rel)
        obs_zone, cand_zone = zones[int(rng.integers(len(zones)))], zones[int(rng.integers(len(zones)))]
        t_obs = round(float(rng.uniform(60, 600)), 1)
        rows.append(
            {
                "pair_id": pid,
                "label": label,
                "observation": {
                    "crop": obs_rel,
                    "description": f"{obs_cls.replace('-', ' ')} seen on camera",
                    "location": obs_zone,
                    "t": t_obs,
                },
                "candidate": {
                    "crop": cand_rel,
                    "label": cand_cls.replace("-", " "),
                    "catalog_id": None if cand_cls == UNKNOWN else cand_cls,
                    "last_location": cand_zone,
                    "last_seen_t": round(t_obs - float(rng.uniform(5, 3600)), 1),
                },
                "context": {"similarity": round(similarity_of(obs, cand), 4), "catalog_id": obs_cls},
                "meta": {"kind": kind, "hard_negative": kind == "hard_negative"},
            }
        )
    path = out / "pairs.jsonl"
    path.write_text("".join(json.dumps(r) + "\n" for r in rows))
    log.info("wrote %d decision pairs to %s", len(rows), path)
    return path
