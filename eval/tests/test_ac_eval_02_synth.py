"""AC-EVAL-2: deterministic synthetic scene generator (appear, move zone, disappear, identical twins, an unknown
object; scripted TTS-free transcripts); emits video frames + gold; same seed => identical output."""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from itertools import combinations, pairwise
from pathlib import Path

import numpy as np
import pytest
import yaml
from conftest import EVAL_DIR, SEED_CATALOG_IDS, SEED_LOCATIONS
from PIL import Image

from nasa_eval.dataset import load_dataset, validate_dataset
from nasa_eval.synth import (
    SUPPORTED_CLASSES,
    Scene,
    SceneConfig,
    TranscriptLine,
    draw_crop,
    generate_scene,
    write_scene,
)

CROP_CLASSES = ["laptop", "drill-pistol", "wrench", "scissors", "duct-tape", "flashlight", "multimeter",
                "camera-dslr", "headset", "tablet"]


@pytest.fixture(scope="module")
def scene() -> Scene:
    return generate_scene(7)


def _dir_digest(d: Path) -> dict[str, str]:
    return {str(p.relative_to(d)): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(d.rglob("*")) if p.is_file()}


# ---------------------------------------------------------------- crops
def test_ac_eval_02_supported_classes_are_catalog_ids():
    assert set(CROP_CLASSES) <= set(SUPPORTED_CLASSES)
    assert set(SUPPORTED_CLASSES) <= SEED_CATALOG_IDS


@pytest.mark.parametrize("cls", CROP_CLASSES + ["unknown"])
def test_ac_eval_02_draw_crop_shape_dtype_and_determinism(cls):
    a = draw_crop(cls, seed=3, size=(64, 48))
    b = draw_crop(cls, seed=3, size=(64, 48))
    assert isinstance(a, np.ndarray) and a.dtype == np.uint8 and a.shape == (48, 64, 3)
    assert np.array_equal(a, b)
    assert a.std() > 5, "crop must not be a flat colour"


def test_ac_eval_02_crops_differ_between_classes():
    crops = {c: draw_crop(c, seed=1) for c in CROP_CLASSES + ["unknown"]}
    for x, y in combinations(crops, 2):
        assert not np.array_equal(crops[x], crops[y]), f"{x} and {y} render identically"


def test_ac_eval_02_seed_varies_crop_but_keeps_class():
    assert not np.array_equal(draw_crop("wrench", seed=1), draw_crop("wrench", seed=2))


def test_ac_eval_02_draw_crop_unknown_class_key_rejected():
    with pytest.raises((KeyError, ValueError)):
        draw_crop("lightsaber", seed=0)


# ---------------------------------------------------------------- scene structure
def test_ac_eval_02_frames_match_config(scene):
    cfg = scene.config
    assert isinstance(scene, Scene) and scene.seed == 7
    assert len(scene.frames) == int(cfg.duration_s * cfg.fps)
    ts = [t for t, _ in scene.frames]
    assert ts[0] == 0 and all(b - a == pytest.approx(1 / cfg.fps) for a, b in pairwise(ts))
    for _, f in scene.frames:
        assert f.dtype == np.uint8 and f.shape == (cfg.height, cfg.width, 3)


def test_ac_eval_02_custom_config_respected():
    cfg = SceneConfig(duration_s=30, fps=2, width=320, height=180)
    s = generate_scene(1, cfg)
    assert len(s.frames) == 60 and s.frames[0][1].shape == (180, 320, 3)


def test_ac_eval_02_gold_locations_and_classes_are_seeded_ids(scene):
    for g in scene.gold:
        assert g.is_unknown or g.catalog_id in SEED_CATALOG_IDS
        for a in g.appearances:
            assert a.location in SEED_LOCATIONS
            assert a.location in scene.zone_regions
        for e in g.events:
            assert e.to_location is None or e.to_location in SEED_LOCATIONS


def test_ac_eval_02_appearance_times_are_frame_times(scene):
    frame_ts = {round(t, 6) for t, _ in scene.frames}
    for g in scene.gold:
        assert g.appearances, g.gold_object_id
        for a in g.appearances:
            assert round(a.t, 6) in frame_ts


def test_ac_eval_02_bbox_inside_its_zone_region(scene):
    for g in scene.gold:
        for a in g.appearances:
            assert a.bbox is not None
            x1, y1, x2, y2 = a.bbox
            assert 0 <= x1 < x2 <= 1 and 0 <= y1 < y2 <= 1
            zx1, zy1, zx2, zy2 = scene.zone_regions[a.location]
            cx, cy = (x1 + x2) / 2, (y1 + y2) / 2
            assert zx1 <= cx <= zx2 and zy1 <= cy <= zy2, (g.gold_object_id, a)


def test_ac_eval_02_objects_are_actually_composited(scene):
    """Pixels inside every gold bbox differ from the empty background (the crop was drawn there)."""
    frames = {round(t, 6): f for t, f in scene.frames}
    h, w = scene.background.shape[:2]
    for g in scene.gold:
        for a in g.appearances:
            f = frames[round(a.t, 6)]
            x1, y1, x2, y2 = a.bbox
            sl = (slice(int(y1 * h), int(y2 * h)), slice(int(x1 * w), int(x2 * w)))
            diff = np.abs(f[sl].astype(int) - scene.background[sl].astype(int)).mean()
            assert diff > 10, (g.gold_object_id, a.t, diff)


def test_ac_eval_02_scenario_has_appear(scene):
    assert any(min(a.t for a in g.appearances) > 0 for g in scene.gold), "some object appears after t=0"


def test_ac_eval_02_scenario_has_move_zone_with_moved_event(scene):
    movers = [g for g in scene.gold if len({a.location for a in g.appearances}) >= 2]
    assert movers
    for g in movers:
        moved = [e for e in g.events if e.type == "moved"]
        assert moved, g.gold_object_id
        e = moved[0]
        before = [a for a in g.appearances if a.t < e.t]
        after = [a for a in g.appearances if a.t >= e.t]
        assert before and after
        assert after[0].location == e.to_location != before[-1].location
        assert e.actor, "moved events carry an actor descriptor"


def test_ac_eval_02_scenario_has_disappear(scene):
    end = scene.config.duration_s - 1 / scene.config.fps
    gone = [g for g in scene.gold if max(a.t for a in g.appearances) < end - 5]
    assert gone, "some object disappears well before the end"
    assert any(e.type == "removed" for g in gone for e in g.events)


def test_ac_eval_02_scenario_has_identical_twins_visible_together(scene):
    by_class: dict[str, list] = {}
    for g in scene.gold:
        if not g.is_unknown:
            by_class.setdefault(g.catalog_id, []).append(g)
    twins = [gs for gs in by_class.values() if len(gs) >= 2]
    assert twins
    a, b = twins[0][:2]
    assert {x.t for x in a.appearances} & {x.t for x in b.appearances}, "twins are concurrently visible"


def test_ac_eval_02_scenario_has_one_unknown(scene):
    unknowns = [g for g in scene.gold if g.is_unknown]
    assert len(unknowns) == 1 and unknowns[0].catalog_id is None


def test_ac_eval_02_unique_gold_ids(scene):
    ids = [g.gold_object_id for g in scene.gold]
    assert len(ids) == len(set(ids))


def test_ac_eval_02_scripted_transcript(scene):
    assert scene.transcript and all(isinstance(x, TranscriptLine) for x in scene.transcript)
    assert all(0 <= x.t < scene.config.duration_s and x.text.strip() for x in scene.transcript)
    mover = next(g for g in scene.gold if any(e.type == "moved" for e in g.events))
    move = next(e for e in mover.events if e.type == "moved")
    near = [x for x in scene.transcript if abs(x.t - move.t) <= 10]
    assert near, "a scripted line accompanies the move"


# ---------------------------------------------------------------- determinism
def test_ac_eval_02_same_seed_identical_scene():
    a, b = generate_scene(11), generate_scene(11)
    assert len(a.frames) == len(b.frames)
    for (ta, fa), (tb, fb) in zip(a.frames, b.frames):
        assert ta == tb and np.array_equal(fa, fb)
    assert a.gold == b.gold and a.transcript == b.transcript


def test_ac_eval_02_different_seed_different_scene():
    a, b = generate_scene(11), generate_scene(12)
    assert any(not np.array_equal(fa, fb) for (_, fa), (_, fb) in zip(a.frames, b.frames)) or a.gold != b.gold


def test_ac_eval_02_written_output_byte_identical(tmp_path):
    d1 = write_scene(generate_scene(5), tmp_path / "a", "synth-5")
    d2 = write_scene(generate_scene(5), tmp_path / "b", "synth-5")
    assert _dir_digest(d1) == _dir_digest(d2)


def test_ac_eval_02_deterministic_across_processes(tmp_path):
    """Same seed in a fresh interpreter with a different PYTHONHASHSEED => byte-identical dataset."""
    code = ("import sys; from nasa_eval.synth import generate_scene, write_scene; "
            "write_scene(generate_scene(5), sys.argv[1], 'synth-5')")
    outs = []
    for i, hashseed in enumerate(("1", "999")):
        out = tmp_path / f"p{i}"
        env = dict(os.environ, PYTHONHASHSEED=hashseed)
        subprocess.run([sys.executable, "-c", code, str(out)], check=True, env=env, cwd=EVAL_DIR)
        outs.append(_dir_digest(out / "synth-5"))
    assert outs[0] == outs[1] and outs[0]


# ---------------------------------------------------------------- emitted dataset
def test_ac_eval_02_written_dataset_is_valid_and_round_trips(tmp_path):
    scene = generate_scene(9, SceneConfig(duration_s=40))
    d = write_scene(scene, tmp_path, "synth-9")
    assert d == tmp_path / "synth-9"
    assert {"manifest.yaml", "gold.jsonl", "transcript.jsonl"} <= {p.name for p in d.iterdir()}
    assert validate_dataset(d, catalog_ids=SEED_CATALOG_IDS, location_ids=SEED_LOCATIONS) == []
    manifest = yaml.safe_load((d / "manifest.yaml").read_text())
    assert manifest["name"] == "synth-9" and manifest["camera"] == scene.config.camera
    assert manifest["audio"] is None, "TTS-free: no audio track"
    data = load_dataset(d)
    assert [g.gold_object_id for g in data.gold] == [g.gold_object_id for g in scene.gold]
    assert data.gold == scene.gold
    frames = list(data.iter_source())
    assert len(frames) == len(scene.frames)
    assert np.array_equal(frames[3][1], scene.frames[3][1]), "frames are stored losslessly"
    assert (frames[3][0] - data.manifest.start_time).total_seconds() == pytest.approx(scene.frames[3][0])
    lines = [json.loads(x) for x in (d / "transcript.jsonl").read_text().splitlines() if x.strip()]
    assert [ln["text"] for ln in lines] == [x.text for x in scene.transcript]


def test_ac_eval_02_frames_written_as_png(tmp_path):
    d = write_scene(generate_scene(2, SceneConfig(duration_s=5)), tmp_path, "s")
    pngs = sorted((d / "frames").glob("*.png"))
    assert len(pngs) == 5
    assert Image.open(pngs[0]).mode == "RGB"


def test_ac_eval_02_cli_generate(tmp_path):
    from nasa_eval.cli import main

    rc = main(["generate", "--seed", "4", "--out", str(tmp_path), "--name", "synth-4"])
    assert rc == 0
    assert validate_dataset(tmp_path / "synth-4") == []
