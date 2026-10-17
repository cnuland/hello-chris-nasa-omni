"""AC-PER-19..23 (real-video dedup failure, cluster integration 2026-10-17).

On real NASA ISS tour footage, Qwen2.5-Omni-7B listed every object once per frame (6 laptops + 6 headsets with
identical boxes in one window, tests/fixtures/omni_iss_tour_window0.json). Crops were cut from the 448 px omni frame,
omni-only boxes were embedded into junk exemplars, and the hint "Columbus Laboratory Module" did not resolve. After
about 6 windows the DB held 10 headset / 7 tablet / 7 camera / 6 laptop entities.

- AC-PER-19 within-window consolidation
- AC-PER-20 prompt: each physical object once per window
- AC-PER-21 full-resolution keyframes for detection and crops
- AC-PER-22 exemplar quality gate + class-and-place matching without an exemplar
- AC-PER-23 location-hint resolution, camera policy, privacy
"""
from __future__ import annotations

import io
import json
from collections import Counter
from datetime import timedelta
from pathlib import Path

import numpy as np
import pytest
from PIL import Image
from support import (
    LOCATIONS,
    T0,
    FakeInventory,
    detections_for,
    obj,
    observation,
    scene_window,
    uri_key,
)
from test_ac_per_08_reconciler import BOX_A, BOX_B, process

from perception.config import ConfigError, PerceptionConfig
from perception.fakes import FakeDetector, FakeEmbedder, FakeOmni, make_frame
from perception.locations import LocationResolver
from perception.media import downscale
from perception.omni import build_system_prompt
from perception.types import Keyframe, OmniResult, Window
from perception.window import WindowBuilder

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "omni_iss_tour_window0.json"
LAPTOP_BOX = (0.21, 0.38, 0.31, 0.52)  # the real omni boxes of the fixture
HEADSET_BOX = (0.24, 0.38, 0.31, 0.45)
PRIVATE_IDS = ("NOD2S5", "NOD2D5", "NOD2P5", "NOD2O5", "NOD3F4")


def real_observation() -> dict:
    return json.loads(FIXTURE.read_text())


def real_omni_result() -> OmniResult:
    """The fixture through FakeOmni, i.e. schema-validated exactly like a real reply."""
    w = scene_window([[]])
    result = FakeOmni([real_observation()], model="Qwen/Qwen2.5-Omni-7B").analyze(w)
    assert result.ok, result.error
    return result


def hires_window(frames_objects, *, t0=T0, step_s=1.0, height=720, width=1280, long_side=448, index=0) -> Window:
    """Window like the WindowBuilder makes it: ``image`` downscaled for omni, ``detect_image`` at full size."""
    frames = []
    for i, objs in enumerate(frames_objects):
        full = make_frame(objs, height=height, width=width)
        frames.append(Keyframe(timestamp=t0 + timedelta(seconds=i * step_s), image=downscale(full, long_side),
                               detect_image=full, pts=i * step_s, index=i))
    return Window(t_start=frames[0].timestamp, t_end=frames[-1].timestamp, frames=frames, index=index)


def instance_counts(payload) -> Counter:
    """Number of observed instances per catalog class in one payload (new or assigned)."""
    out: Counter = Counter()
    for a in payload["assignments"]:
        cid = (a.get("new_entity") or {}).get("catalog_id") if a.get("entity_id") is None else "existing"
        out[cid] += 1
    return out


def live_counts(inv: FakeInventory) -> Counter:
    return Counter(e["catalog_id"] for e in inv.live())


def crop_sizes(storage) -> list[tuple[int, int]]:
    sizes = []
    for key, data in storage.objects.items():
        if key.startswith("crops/"):
            with Image.open(io.BytesIO(data)) as img:
                sizes.append(img.size)  # (w, h)
    return sizes


TOUR_FRAMES = [[("laptop-1", LAPTOP_BOX), ("headset-1", HEADSET_BOX)] for _ in range(6)]
TOUR_LABELS = {"laptop-1": "laptop", "headset-1": "headset"}


# ================================================================================ real-data regression (AC-19/22/23)
def test_ac_per_19_real_fixture_is_the_measured_failure():
    obs = real_observation()
    labels = Counter(o["label"] for o in obs["objects"])
    assert labels == {"laptop": 6, "headset": 6}
    assert sorted(o["frame_index"] for o in obs["objects"] if o["label"] == "laptop") == [0, 1, 2, 3, 4, 5]
    assert obs["location_hint"] == "Columbus Laboratory Module"


def test_ac_per_19_real_omni_window_gives_one_laptop_and_one_headset_handheld(make_reconciler, inv):
    """The real omni reply without detector links (as on the cluster): ONE laptop and ONE headset, in COL1."""
    rec = make_reconciler(camera_id="cam-handheld")
    out = rec.process(scene_window([[]] * 6), real_omni_result(), [[] for _ in range(6)])
    assert out.status == "posted", out.error
    assert instance_counts(inv.last) == {"laptop": 1, "headset": 1}
    assert live_counts(inv) == {"laptop": 1, "headset": 1}
    assert {a["sighting"]["location_id"] for a in inv.last["assignments"]} == {"COL1"}
    assert {e["last_location"] for e in inv.live()} == {"COL1"}


def test_ac_per_22_real_omni_only_boxes_give_no_exemplar(make_reconciler, inv, storage):
    rec = make_reconciler(camera_id="cam-handheld")
    rec.process(scene_window([[]] * 6), real_omni_result(), [[] for _ in range(6)])
    for a in inv.last["assignments"]:
        assert a["exemplar"] is None
        assert a["sighting"]["bbox"] is not None  # the sighting is still recorded, with omni's box
        assert a["sighting"].get("frame_uri")
    assert not any(k.startswith("crops/") for k in storage.objects)
    assert inv.candidate_calls == []  # junk appearance vectors are never used for retrieval


def test_ac_per_19_real_omni_window_with_detector_boxes_and_hires_crops(make_reconciler, inv, storage):
    """With OWLv2-style boxes on the full-resolution frames: one instance each, real exemplars, crops cut from the
    1280x720 frame (the headset crop is 90x50 px there; it was 31x18 px on the 448 px omni frame)."""
    rec = make_reconciler(camera_id="cam-handheld")
    out = rec.process(hires_window(TOUR_FRAMES), real_omni_result(), detections_for(TOUR_FRAMES, TOUR_LABELS))
    assert out.status == "posted", out.error
    assert instance_counts(inv.last) == {"laptop": 1, "headset": 1}
    assert all(a["exemplar"] is not None for a in inv.last["assignments"])
    sizes = crop_sizes(storage)
    assert len(sizes) == 2
    assert all(w >= 88 and h >= 49 for w, h in sizes), sizes  # laptop ~129x102, headset ~90x51 px


@pytest.mark.parametrize("with_detections", [False, True])
def test_ac_per_19_real_omni_six_windows_keep_one_laptop_and_one_headset(make_reconciler, inv, with_detections):
    """The cluster run: ~6 windows of the same scene. The DB must end with one laptop and one headset."""
    rec = make_reconciler(camera_id="cam-handheld")
    for k in range(6):
        t0 = T0 + timedelta(seconds=4 * k)
        if with_detections:
            w, dets = hires_window(TOUR_FRAMES, t0=t0, index=k), detections_for(TOUR_FRAMES, TOUR_LABELS)
        else:
            w, dets = scene_window([[]] * 6, t0=t0, index=k), [[] for _ in range(6)]
        assert rec.process(w, real_omni_result(), dets).status == "posted"
    assert live_counts(inv) == {"laptop": 1, "headset": 1}, live_counts(inv)
    assert not [r for r in inv.reviews if r["kind"] == "possible_duplicate"]


def test_ac_per_23_real_fixture_on_fixed_lab_camera_uses_hint_but_flags_low_confidence(make_reconciler, inv):
    rec = make_reconciler(camera_id="cam-lab1-fwd")
    rec.process(scene_window([[]] * 6), real_omni_result(), [[] for _ in range(6)])
    assert instance_counts(inv.last) == {"laptop": 1, "headset": 1}
    for a in inv.last["assignments"]:
        assert a["sighting"]["location_id"] == "COL1"
        assert a["sighting"]["confidence"] <= 0.5


# ================================================================================ AC-PER-19 consolidation
LABELS = {"laptop-A": "laptop", "laptop-B": "laptop", "orb-1": "glowing orb", "tablet-1": "tablet"}


def _n_assignments(inv) -> int:
    return len(inv.last["assignments"])


def test_ac_per_19_two_laptops_side_by_side_stay_separate(make_reconciler, inv):
    frames = [[("laptop-A", BOX_A), ("laptop-B", BOX_B)]]
    process(make_reconciler(), frames, [obj("laptop", BOX_A), obj("laptop", BOX_B)], LABELS)
    assert _n_assignments(inv) == 2


def test_ac_per_19_two_adjacent_laptops_touching_stay_separate(make_reconciler, inv):
    left, right = (0.10, 0.10, 0.30, 0.40), (0.30, 0.10, 0.50, 0.40)
    frames = [[("laptop-A", left), ("laptop-B", right)]]
    process(make_reconciler(), frames, [obj("laptop", left), obj("laptop", right)], LABELS)
    assert _n_assignments(inv) == 2


def test_ac_per_19_two_laptops_listed_in_different_frames_stay_separate(make_reconciler, inv):
    """AC-PER-20 asks for one listing per object at its clearest frame: different frames + disjoint boxes = 2."""
    frames = [[("laptop-A", BOX_A), ("laptop-B", BOX_B)]] * 2
    process(make_reconciler(), frames, [obj("laptop", BOX_A, frame_index=0), obj("laptop", BOX_B, frame_index=1)],
            LABELS)
    assert _n_assignments(inv) == 2


def test_ac_per_19_same_laptop_listed_per_frame_with_jittered_boxes_is_one(make_reconciler, inv):
    jitter = [(0.10, 0.10, 0.30, 0.40), (0.11, 0.10, 0.31, 0.41), (0.10, 0.11, 0.29, 0.40)]
    frames = [[("laptop-A", b)] for b in jitter]
    process(make_reconciler(), frames, [obj("laptop", b, frame_index=i) for i, b in enumerate(jitter)], LABELS)
    assert _n_assignments(inv) == 1


def test_ac_per_19_iou_just_below_threshold_stays_separate(make_reconciler, inv):
    a, b = (0.0, 0.0, 0.4, 0.4), (0.2, 0.0, 0.6, 0.4)  # IoU = 1/3
    frames = [[("laptop-A", a)], [("laptop-B", b)]]
    process(make_reconciler(), frames, [obj("laptop", a, frame_index=0), obj("laptop", b, frame_index=1)], LABELS)
    assert _n_assignments(inv) == 2


def test_ac_per_19_threshold_is_configurable(make_reconciler, inv):
    a, b = (0.0, 0.0, 0.4, 0.4), (0.2, 0.0, 0.6, 0.4)  # IoU = 1/3
    frames = [[("laptop-A", a)], [("laptop-B", b)]]
    process(make_reconciler(consolidate_iou=0.3), frames,
            [obj("laptop", a, frame_index=0), obj("laptop", b, frame_index=1)], LABELS)
    assert _n_assignments(inv) == 1


def test_ac_per_19_clustering_is_not_transitive(make_reconciler, inv):
    """A~B and B~C (IoU >= 0.5) but A !~ C: the leader A keeps B; C starts its own instance (no chaining)."""
    a, b, c = (0.00, 0.0, 0.30, 0.3), (0.06, 0.0, 0.36, 0.3), (0.12, 0.0, 0.42, 0.3)
    frames = [[("laptop-A", a)], [("laptop-A", b)], [("laptop-A", c)]]
    objs = [obj("laptop", a, frame_index=0, confidence=0.95), obj("laptop", b, frame_index=1, confidence=0.9),
            obj("laptop", c, frame_index=2, confidence=0.8)]
    process(make_reconciler(), frames, objs, LABELS)
    assert _n_assignments(inv) == 2


def test_ac_per_19_boxless_repeats_collapse(make_reconciler, inv):
    frames = [[("laptop-A", BOX_A)]] * 3
    process(make_reconciler(), frames, [obj("laptop", None, frame_index=i) for i in range(3)], LABELS)
    assert _n_assignments(inv) == 1


def test_ac_per_19_two_boxless_laptops_in_the_same_frame_stay_separate(make_reconciler, inv):
    frames = [[("laptop-A", BOX_A), ("laptop-B", BOX_B)]]
    process(make_reconciler(), frames, [obj("laptop", None, frame_index=0), obj("laptop", None, frame_index=0)],
            LABELS)
    assert _n_assignments(inv) == 2


def test_ac_per_19_different_classes_with_the_same_box_stay_separate(make_reconciler, inv):
    frames = [[("laptop-A", BOX_A)]]
    process(make_reconciler(), frames, [obj("laptop", BOX_A), obj("tablet", BOX_A)], LABELS)
    assert _n_assignments(inv) == 2


def test_ac_per_19_unknown_same_normalised_label_collapses(make_reconciler, inv):
    frames = [[("orb-1", BOX_B)]] * 2
    process(make_reconciler(), frames, [obj("Glowing Orb", BOX_B, frame_index=0), obj("glowing orb", BOX_B,
                                                                                       frame_index=1)], LABELS)
    assert _n_assignments(inv) == 1
    assert len([r for r in inv.reviews if r["kind"] == "unknown_object"]) == 1


def test_ac_per_19_unknown_different_labels_stay_separate(make_reconciler, inv):
    frames = [[("orb-1", BOX_B)]]
    process(make_reconciler(), frames, [obj("glowing orb", BOX_B), obj("strange cylinder", BOX_B)], LABELS)
    assert _n_assignments(inv) == 2


def test_ac_per_19_fungible_items_are_not_consolidated(make_reconciler, inv):
    """Two sample bags in a pile (overlapping boxes) still count 2 (AC-PER-9 per-frame counting)."""
    a, b = (0.10, 0.10, 0.30, 0.30), (0.12, 0.10, 0.32, 0.30)
    process(make_reconciler(), [[]], [obj("sample bag", a), obj("sample bag", b)], {})
    assert [s["est_count"] for s in inv.last["stock_updates"]] == [2]


def test_ac_per_19_keeps_the_detector_linked_member(make_reconciler, inv):
    """Listed in frames 0..2, detected only in frame 2: the kept instance is frame 2 with the detector's box."""
    det_box = (0.12, 0.12, 0.30, 0.40)
    frames = [[], [], [("laptop-A", det_box)]]
    objs = [obj("laptop", BOX_A, frame_index=i, confidence=0.99 if i == 0 else 0.6) for i in range(3)]
    process(make_reconciler(), frames, objs, LABELS)
    (a,) = inv.last["assignments"]
    assert a["sighting"]["seen_at"] == (T0 + timedelta(seconds=2)).isoformat()
    assert a["sighting"]["bbox"] == pytest.approx(list(det_box), abs=0.01)
    assert a["exemplar"] is not None


def test_ac_per_19_ties_keep_the_latest_frame(make_reconciler, inv):
    frames = [[("laptop-A", BOX_A)]] * 3
    process(make_reconciler(), frames, [obj("laptop", BOX_A, frame_index=i) for i in range(3)], LABELS)
    (a,) = inv.last["assignments"]
    assert a["sighting"]["seen_at"] == (T0 + timedelta(seconds=2)).isoformat()


def test_ac_per_19_consolidated_instance_keeps_held_by_from_any_member(make_reconciler, inv):
    """held_by on a lower-confidence member still marks the instance as carried (no impossible-transit gate)."""
    vec = FakeEmbedder().vector_for_object("laptop-A")
    eid = inv.add_entity(catalog_id="laptop", embedding=vec, last_location="NOD2", last_seen_at=T0)
    frames = [[("laptop-A", BOX_A)]] * 2
    objs = [obj("laptop", BOX_A, frame_index=0, confidence=0.95),
            obj("laptop", BOX_A, frame_index=1, confidence=0.5, held_by="P1")]
    process(make_reconciler(), frames, objs, LABELS, t0=T0 + timedelta(seconds=5))
    (a,) = inv.last["assignments"]
    assert a["entity_id"] == eid  # carried from NOD2 to LAB1 in 5 s is allowed only for a held object


# ================================================================================ AC-PER-20 prompt
def test_ac_per_20_prompt_lists_each_object_once_at_its_clearest_frame():
    prompt = build_system_prompt(["laptop"]).lower()
    assert "once per window" in prompt
    assert "each distinct physical object" in prompt
    assert "frame_index = the frame where it is clearest" in prompt


def test_ac_per_20_prompt_location_hint_is_where_the_camera_is():
    prompt = build_system_prompt(["laptop"]).lower()
    assert "where the camera is" in prompt
    assert "not a destination" in prompt


# ================================================================================ AC-PER-21 full-resolution frames
def _kf(h, w, sec=0.0):
    return Keyframe(timestamp=T0 + timedelta(seconds=sec), image=np.zeros((h, w, 3), dtype=np.uint8), pts=sec)


def test_ac_per_21_builder_keeps_capped_full_resolution_frame():
    (win,) = WindowBuilder(long_side=448, detect_long_side=1280).push(_kf(1080, 1920))
    f = win.frames[0]
    assert f.image.shape == (252, 448, 3)
    assert f.hires.shape == (720, 1280, 3)


def test_ac_per_21_builder_default_detect_long_side_is_1280():
    assert WindowBuilder().detect_long_side == 1280
    (win,) = WindowBuilder().push(_kf(1080, 1920))
    assert win.frames[0].hires.shape == (720, 1280, 3)


def test_ac_per_21_frame_below_cap_is_kept_without_copy():
    item = _kf(720, 1280)
    (win,) = WindowBuilder(detect_long_side=1280).push(item)
    assert win.frames[0].hires is item.image


def test_ac_per_21_small_frame_has_a_single_image():
    item = _kf(240, 320)
    (win,) = WindowBuilder(detect_long_side=1280).push(item)
    assert win.frames[0].hires is win.frames[0].image


def test_ac_per_21_detect_long_side_zero_uses_the_omni_image():
    (win,) = WindowBuilder(detect_long_side=0).push(_kf(1080, 1920))
    assert win.frames[0].hires is win.frames[0].image


def test_ac_per_21_detect_image_never_smaller_than_omni_image():
    (win,) = WindowBuilder(long_side=448, detect_long_side=200).push(_kf(1080, 1920))
    assert win.frames[0].hires.shape == win.frames[0].image.shape == (252, 448, 3)


def test_ac_per_21_builder_retains_at_most_window_frames_hires_frames():
    b = WindowBuilder(n_frames=3, stride_s=1.0)
    for s in range(10):
        b.push(_kf(1080, 1920, s))
    assert len(b._frames) == 3


def test_ac_per_21_keyframe_without_detect_image_falls_back_to_image():
    k = _kf(10, 20)
    assert k.hires is k.image


@pytest.mark.parametrize("h,w", [(720, 1280), (1280, 720), (1000, 333)])
def test_ac_per_21_crop_is_cut_from_full_resolution_with_scaled_box(make_reconciler, inv, storage, h, w):
    box = (0.25, 0.25, 0.50, 0.75)
    win = hires_window([[("laptop-A", box)]], height=h, width=w)
    rec = make_reconciler()
    omni = OmniResult(status="ok", observation=observation([obj("laptop", box)]), model="m", raw="{}")
    rec.process(win, omni, detections_for([[("laptop-A", box)]], {"laptop-A": "laptop"}))
    (size,) = crop_sizes(storage)
    assert abs(size[0] - 0.25 * w) <= 2 and abs(size[1] - 0.5 * h) <= 2, (size, w, h)


@pytest.mark.parametrize("h,w", [(1080, 1920), (1920, 1080), (1000, 333), (481, 1999)])
def test_ac_per_21_normalised_boxes_agree_between_omni_and_hires_images(h, w):
    """No letterbox/padding: the same painted object has the same normalised box on both images (<= 1.5 px of the
    omni image)."""
    box = (0.31, 0.27, 0.58, 0.66)
    item = Keyframe(timestamp=T0, image=make_frame([("o", box)], height=h, width=w))
    (win,) = WindowBuilder(long_side=448, detect_long_side=1280).push(item)
    f = win.frames[0]
    det = FakeDetector({"o": "laptop"})
    on_full = det.detect(f.hires, ["laptop"])
    # bilinear downscale blends edges: find the object on the omni image by its dominant interior colour
    small = f.image.astype(int)
    colour = np.asarray(make_frame([("o", (0, 0, 1, 1))], height=1, width=1)[0, 0], dtype=int)
    mask = np.abs(small - colour).sum(axis=-1) <= 30
    ys, xs = np.flatnonzero(mask.any(axis=1)), np.flatnonzero(mask.any(axis=0))
    sh, sw = small.shape[:2]
    on_small = (xs[0] / sw, ys[0] / sh, (xs[-1] + 1) / sw, (ys[-1] + 1) / sh)
    tol = 1.5 / 448 + 1.0 / min(sh, sw)
    assert on_full and all(abs(a - b) <= tol for a, b in zip(on_full[0].bbox, on_small, strict=True))
    assert all(abs(a - b) <= 2.0 / min(f.hires.shape[:2]) for a, b in zip(on_full[0].bbox, box, strict=True))


def test_ac_per_21_replay_detects_on_hires_and_sends_downscaled_frames_to_omni(inv, storage, config):
    import perception.replay
    from perception.fakes import FakeASR, FakeDecision

    det = FakeDetector({"laptop-A": "laptop"})
    omni = FakeOmni([observation([obj("laptop", BOX_A)])])
    frame = make_frame([("laptop-A", BOX_A)], height=1080, width=1920)
    config.window_frames, config.stride_s = 1, 1.0
    perception.replay.run([(T0, frame, None)], camera_id="cam-lab1-fwd", start_time=T0, api=inv, omni=omni,
                          detector=det, embedder=FakeEmbedder(), decision=FakeDecision(), asr=FakeASR(),
                          storage=storage, config=config)
    assert det.calls[0][0].shape == (720, 1280, 3)
    assert omni.calls[0].frames[0].image.shape == (252, 448, 3)
    (size,) = crop_sizes(storage)
    assert abs(size[0] - 0.2 * 1280) <= 2 and abs(size[1] - 0.3 * 720) <= 2


def test_ac_per_21_replay_honours_detect_long_side_config(inv, storage, config):
    import perception.replay
    from perception.fakes import FakeASR, FakeDecision

    det = FakeDetector({"laptop-A": "laptop"})
    frame = make_frame([("laptop-A", BOX_A)], height=1080, width=1920)
    config.window_frames, config.stride_s, config.detect_long_side = 1, 1.0, 960
    perception.replay.run([(T0, frame, None)], camera_id="cam-lab1-fwd", start_time=T0, api=inv,
                          omni=FakeOmni([observation([obj("laptop", BOX_A)])]), detector=det,
                          embedder=FakeEmbedder(), decision=FakeDecision(), asr=FakeASR(), storage=storage,
                          config=config)
    assert det.calls[0][0].shape == (540, 960, 3)


# ================================================================================ AC-PER-22 quality gate
def test_ac_per_22_omni_only_object_records_sighting_without_exemplar(make_reconciler, inv, storage):
    process(make_reconciler(), [[]], [obj("laptop", BOX_A)], {})  # the detector found nothing
    (a,) = inv.last["assignments"]
    assert a["exemplar"] is None
    assert a["sighting"]["location_id"] == "LAB1" and a["sighting"]["bbox"] == pytest.approx(list(BOX_A))
    assert a["sighting"].get("frame_uri")
    assert not any(k.startswith("crops/") for k in storage.objects)


def test_ac_per_22_tiny_detector_crop_gets_no_exemplar(make_reconciler, inv, storage):
    tiny = (0.50, 0.50, 0.52, 0.52)  # 13 x 10 px on 640 x 480
    process(make_reconciler(), [[("laptop-A", tiny)]], [obj("laptop", tiny)], {"laptop-A": "laptop"})
    (a,) = inv.last["assignments"]
    assert a["exemplar"] is None
    assert not any(k.startswith("crops/") for k in storage.objects)


@pytest.mark.parametrize("px,kept", [(28, False), (36, True)])
def test_ac_per_22_min_crop_px_boundary(make_reconciler, inv, px, kept):
    box = (0.30, 0.30, 0.30 + 100 / 640, 0.30 + px / 480)  # short side ~px (+1 for ceil)
    process(make_reconciler(), [[("laptop-A", box)]], [obj("laptop", box)], {"laptop-A": "laptop"})
    (a,) = inv.last["assignments"]
    assert (a["exemplar"] is not None) is kept


def test_ac_per_22_min_crop_px_is_configurable(make_reconciler, inv):
    tiny = (0.50, 0.50, 0.52, 0.52)
    process(make_reconciler(min_crop_px=0), [[("laptop-A", tiny)]], [obj("laptop", tiny)], {"laptop-A": "laptop"})
    assert inv.last["assignments"][0]["exemplar"] is not None


def test_ac_per_22_gated_sighting_attaches_to_recent_same_class_entity_in_the_module(make_reconciler, inv):
    eid = inv.add_entity(catalog_id="laptop", embedding=FakeEmbedder().vector_for_object("laptop-A"),
                         last_location="LAB1S2", last_seen_at=T0 - timedelta(seconds=60))
    process(make_reconciler(), [[]], [obj("laptop", BOX_A)], {})
    (a,) = inv.last["assignments"]
    assert a["entity_id"] == eid and a["exemplar"] is None
    assert inv.candidate_calls == []
    assert live_counts(inv) == {"laptop": 1}


def test_ac_per_22_gated_sighting_prefers_the_most_recent_entity(make_reconciler, inv):
    inv.add_entity(catalog_id="laptop", last_location="LAB1", last_seen_at=T0 - timedelta(seconds=300))
    recent = inv.add_entity(catalog_id="laptop", last_location="LAB1", last_seen_at=T0 - timedelta(seconds=30))
    process(make_reconciler(), [[]], [obj("laptop", BOX_A)], {})
    assert inv.last["assignments"][0]["entity_id"] == recent


def test_ac_per_22_gated_sighting_in_another_module_is_new(make_reconciler, inv):
    inv.add_entity(catalog_id="laptop", last_location="NOD2", last_seen_at=T0 - timedelta(seconds=60))
    process(make_reconciler(), [[]], [obj("laptop", BOX_A)], {})
    a = inv.last["assignments"][0]
    assert a["entity_id"] is None and a["new_entity"]["catalog_id"] == "laptop"


def test_ac_per_22_gated_sighting_after_the_horizon_is_new(make_reconciler, inv):
    inv.add_entity(catalog_id="laptop", last_location="LAB1", last_seen_at=T0 - timedelta(seconds=601))
    process(make_reconciler(), [[]], [obj("laptop", BOX_A)], {})
    assert inv.last["assignments"][0]["entity_id"] is None


def test_ac_per_22_gated_sighting_horizon_is_configurable(make_reconciler, inv):
    eid = inv.add_entity(catalog_id="laptop", last_location="LAB1", last_seen_at=T0 - timedelta(seconds=3000))
    process(make_reconciler(no_exemplar_match_s=3600), [[]], [obj("laptop", BOX_A)], {})
    assert inv.last["assignments"][0]["entity_id"] == eid


def test_ac_per_22_gated_sighting_ignores_other_classes_and_merged(make_reconciler, inv):
    inv.add_entity(catalog_id="tablet", last_location="LAB1", last_seen_at=T0)
    inv.add_entity(catalog_id="laptop", last_location="LAB1", last_seen_at=T0, status="merged")
    process(make_reconciler(), [[]], [obj("laptop", BOX_A)], {})
    assert inv.last["assignments"][0]["entity_id"] is None


def test_ac_per_22_two_gated_laptops_cannot_take_the_same_entity(make_reconciler, inv):
    eid = inv.add_entity(catalog_id="laptop", last_location="LAB1", last_seen_at=T0 - timedelta(seconds=10))
    process(make_reconciler(), [[]], [obj("laptop", BOX_A), obj("laptop", BOX_B)], {})
    ids = [a["entity_id"] for a in inv.last["assignments"]]
    assert sorted(ids, key=str) == sorted([eid, None], key=str)


def test_ac_per_22_embedded_match_takes_precedence_over_gated_match(make_reconciler, inv):
    """A detector-linked laptop matched by appearance keeps its entity; the gated laptop does not steal it."""
    vec = FakeEmbedder().vector_for_object("laptop-A")
    eid = inv.add_entity(catalog_id="laptop", embedding=vec, last_location="LAB1", last_seen_at=T0)
    frames = [[("laptop-A", BOX_A)]]
    process(make_reconciler(), frames, [obj("laptop", BOX_B, confidence=0.99), obj("laptop", BOX_A, confidence=0.5)],
            {"laptop-A": "laptop"})
    by_box = {tuple(round(v, 2) for v in a["sighting"]["bbox"]): a for a in inv.last["assignments"]}
    assert by_box[tuple(round(v, 2) for v in BOX_A)]["entity_id"] == eid
    assert by_box[tuple(round(v, 2) for v in BOX_B)]["entity_id"] is None


def test_ac_per_22_gated_unknown_matches_same_label_unknown_entity(make_reconciler, inv):
    eid = inv.add_entity(catalog_id=None, label="glowing orb", status="unknown", last_location="LAB1",
                         last_seen_at=T0 - timedelta(seconds=30))
    process(make_reconciler(), [[]], [obj("Glowing  Orb", BOX_B)], {})
    assert inv.last["assignments"][0]["entity_id"] == eid
    assert not [r for r in inv.last["reviews"] if r["kind"] == "unknown_object"]


def test_ac_per_22_gated_unknown_without_match_reviews_with_the_frame_image(make_reconciler, inv):
    process(make_reconciler(), [[]], [obj("strange cylinder", BOX_B)], {})
    (rv,) = [r for r in inv.last["reviews"] if r["kind"] == "unknown_object"]
    assert uri_key(rv["image_uri"]).startswith("frames/")


def test_ac_per_22_expected_count_review_for_gated_instance_uses_the_frame(make_reconciler, inv):
    for i in range(12):  # laptop expected_count = 12
        inv.add_entity(catalog_id="laptop", last_location="NOD2", last_seen_at=T0 - timedelta(hours=2, seconds=i))
    process(make_reconciler(), [[]], [obj("laptop", BOX_A)], {})
    (rv,) = [r for r in inv.last["reviews"] if r["kind"] == "possible_duplicate"]
    assert uri_key(rv["image_uri"]).startswith("frames/")


def test_ac_per_22_detector_linked_exemplar_quality_is_detector_score(make_reconciler, inv):
    process(make_reconciler(), [[("laptop-A", BOX_A)]], [obj("laptop", BOX_A)], {"laptop-A": "laptop"})
    ex = inv.last["assignments"][0]["exemplar"]
    assert ex is not None and ex["quality"] == pytest.approx(0.9) and uri_key(ex["crop_uri"]).startswith("crops/")


# ================================================================================ AC-PER-23 location hints
@pytest.fixture()
def resolver():
    return LocationResolver(LOCATIONS)


@pytest.mark.parametrize("hint,expected", [
    ("Columbus Laboratory Module", "COL1"), ("COLUMBUS LABORATORY MODULE", "COL1"), ("columbus module", "COL1"),
    ("the Columbus module of the ISS", "COL1"), ("inside the Columbus module aboard the International Space Station",
                                                 "COL1"),
    ("Destiny Laboratory", "LAB1"), ("US Laboratory", "LAB1"), ("U.S. Laboratory Module", "LAB1"),
    ("Destiny laboratory module", "LAB1"), ("Kibo laboratory module", "JPM1"), ("Node 2 module", "NOD2"),
    ("the Harmony module", "NOD2"), ("Zvezda Service Module", "ZVEZDA"), ("Cupola module", "CUP1"),
])
def test_ac_per_23_relaxed_hint_resolution(resolver, hint, expected):
    assert resolver.resolve(hint) == expected


@pytest.mark.parametrize("hint", ["laboratory module", "International Space Station", "module", "the module",
                                  "laboratory", "space station module"])
def test_ac_per_23_filler_only_hints_do_not_resolve(resolver, hint):
    assert resolver.resolve(hint) is None


def test_ac_per_23_ambiguous_relaxed_key_resolves_to_nothing():
    locs = [{"location_id": "A1", "parent_id": None, "kind": "module", "display_name": "Alpha Module",
             "no_capture": False},
            {"location_id": "A2", "parent_id": None, "kind": "module", "display_name": "Alpha Laboratory",
             "no_capture": False}]
    r = LocationResolver(locs)
    assert r.resolve("Alpha Module") == "A1" and r.resolve("Alpha Laboratory") == "A2"
    assert r.resolve("alpha") is None and r.resolve("the alpha laboratory module") is None


@pytest.mark.parametrize("hint", ["Node 2 crew quarters", "crew quarters", "the crew quarters module",
                                  "Node 3 WHC", "WHC", "the toilet", "waste and hygiene compartment",
                                  "Node 3 WHC (toilet) module", "sleep station in Harmony", "NOD2S5", "NOD3F4",
                                  "Node 2 crew quarters (port) module"])
def test_ac_per_23_private_hint_is_detected(resolver, hint):
    assert resolver.is_private_hint(hint) is True


@pytest.mark.parametrize("hint", [None, "", "Node 2", "Lab port rack 3", "port rack", "Columbus Laboratory Module",
                                  "Harmony", "deck"])
def test_ac_per_23_ordinary_hint_is_not_private(resolver, hint):
    assert resolver.is_private_hint(hint) is False


@pytest.mark.parametrize("camera", ["USOS", "LAB1", "NOD2", "COL1A1"])
@pytest.mark.parametrize("hint", ["Node 2 crew quarters", "crew quarters module", "Node 3 WHC", "toilet",
                                  *PRIVATE_IDS, *(f"{loc['display_name']} module" for loc in LOCATIONS
                                                  if loc["location_id"] in PRIVATE_IDS)])
def test_ac_per_23_private_hint_makes_any_camera_window_private(resolver, camera, hint):
    _location, no_capture = resolver.window_scope(camera, hint)
    assert no_capture is True


def test_ac_per_23_every_no_capture_location_is_covered():
    assert {loc["location_id"] for loc in LOCATIONS if loc.get("no_capture")} == set(PRIVATE_IDS)


def test_ac_per_23_roaming_camera_takes_the_hint(resolver):
    scope = resolver.window_policy("USOS", "Columbus Laboratory Module")
    assert (scope.location, scope.no_capture, scope.hint_conflict) == ("COL1", False, False)


def test_ac_per_23_fixed_camera_hint_in_another_module_is_a_conflict(resolver):
    scope = resolver.window_policy("LAB1", "Columbus Laboratory Module")
    assert (scope.location, scope.no_capture, scope.hint_conflict) == ("COL1", False, True)


def test_ac_per_23_fixed_camera_hint_inside_its_module_refines_without_conflict(resolver):
    scope = resolver.window_policy("LAB1", "Lab starboard rack 2")
    assert (scope.location, scope.no_capture, scope.hint_conflict) == ("LAB1S2", False, False)
    scope = resolver.window_policy("LAB1S2", "Destiny")
    assert (scope.location, scope.hint_conflict) == ("LAB1S2", False)


@pytest.mark.parametrize("camera", ["LAB1", "USOS"])
def test_ac_per_23_ancestor_hint_never_coarsens_the_camera_location(resolver, camera):
    scope = resolver.window_policy(camera, "US segment")
    assert scope.location == camera and scope.hint_conflict is False


def test_ac_per_23_window_scope_is_policy_location_and_privacy(resolver):
    assert resolver.window_scope("USOS", "Columbus Laboratory Module") == ("COL1", False)
    assert resolver.window_scope("LAB1", "Node 2 crew quarters") == ("LAB1", True)


def test_ac_per_23_handheld_hint_sets_window_location(make_reconciler, inv):
    process(make_reconciler(camera_id="cam-handheld"), [[("laptop-A", BOX_A)]], [obj("laptop", BOX_A)],
            {"laptop-A": "laptop"}, location_hint="Columbus Laboratory Module")
    a = inv.last["assignments"][0]
    assert a["sighting"]["location_id"] == "COL1" and a["sighting"]["confidence"] == pytest.approx(0.9)


def test_ac_per_23_fixed_camera_conflicting_hint_caps_confidence(make_reconciler, inv):
    process(make_reconciler(camera_id="cam-lab1-fwd"), [[("laptop-A", BOX_A)]], [obj("laptop", BOX_A)],
            {"laptop-A": "laptop"}, location_hint="Columbus")
    s = inv.last["assignments"][0]["sighting"]
    assert s["location_id"] == "COL1" and s["confidence"] == pytest.approx(0.5)


def test_ac_per_23_conflict_cap_is_configurable(make_reconciler, inv):
    process(make_reconciler(camera_id="cam-lab1-fwd", hint_conflict_confidence=0.3), [[("laptop-A", BOX_A)]],
            [obj("laptop", BOX_A)], {"laptop-A": "laptop"}, location_hint="Columbus")
    assert inv.last["assignments"][0]["sighting"]["confidence"] == pytest.approx(0.3)


def test_ac_per_23_fixed_camera_hint_inside_module_keeps_confidence(make_reconciler, inv):
    process(make_reconciler(camera_id="cam-lab1-fwd"), [[("laptop-A", BOX_A)]], [obj("laptop", BOX_A)],
            {"laptop-A": "laptop"}, location_hint="Lab starboard rack 2")
    assert inv.last["assignments"][0]["sighting"]["confidence"] == pytest.approx(0.9)


@pytest.mark.parametrize("camera_id", ["cam-handheld", "cam-lab1-fwd", "cam-nod2-aft"])
@pytest.mark.parametrize("hint", ["Node 2 crew quarters", "the crew quarters module", "Node 3 WHC module",
                                  "Node 3 WHC (toilet) module"])
def test_ac_per_23_private_hint_window_stores_nothing(make_reconciler, inv, storage, camera_id, hint):
    process(make_reconciler(camera_id=camera_id), [[("laptop-A", BOX_A), ("orb-1", BOX_B)]],
            [obj("laptop", BOX_A), obj("glowing orb", BOX_B)], {"laptop-A": "laptop", "orb-1": "glowing orb"},
            location_hint=hint)
    assert storage.objects == {}, list(storage.objects)
    p = inv.last
    assert p.get("reviews", []) == []
    assert p["window"].get("raw_uri") is None and p["window"]["transcript"] == ""
    for a in p["assignments"]:
        assert a["sighting"].get("frame_uri") is None
        assert (a.get("exemplar") or {}).get("crop_uri") is None


# ================================================================================ config
def test_ac_per_21_22_23_config_defaults():
    c = PerceptionConfig()
    assert c.detect_long_side == 1280
    assert c.min_crop_px == 32
    assert c.consolidate_iou == 0.5
    assert c.no_exemplar_match_s == 600.0
    assert c.hint_conflict_confidence == 0.5


def test_ac_per_21_22_23_config_from_env():
    c = PerceptionConfig.from_env({"PERCEPTION_DETECT_LONG_SIDE": "960", "PERCEPTION_MIN_CROP_PX": "48",
                                   "PERCEPTION_CONSOLIDATE_IOU": "0.6", "PERCEPTION_NO_EXEMPLAR_MATCH_S": "120",
                                   "PERCEPTION_HINT_CONFLICT_CONFIDENCE": "0.4"})
    assert (c.detect_long_side, c.min_crop_px, c.consolidate_iou) == (960, 48, 0.6)
    assert (c.no_exemplar_match_s, c.hint_conflict_confidence) == (120.0, 0.4)
    assert PerceptionConfig.from_env({"PERCEPTION_DETECT_LONG_SIDE": "0"}).detect_long_side == 0


@pytest.mark.parametrize("var,value", [
    ("PERCEPTION_DETECT_LONG_SIDE", "big"), ("PERCEPTION_DETECT_LONG_SIDE", "-1"),
    ("PERCEPTION_DETECT_LONG_SIDE", "4097"), ("PERCEPTION_DETECT_LONG_SIDE", "12.5"),
    ("PERCEPTION_MIN_CROP_PX", "x"), ("PERCEPTION_CONSOLIDATE_IOU", "1.5"),
    ("PERCEPTION_HINT_CONFLICT_CONFIDENCE", "2"), ("PERCEPTION_NO_EXEMPLAR_MATCH_S", "-5"),
])
def test_ac_per_21_22_23_config_rejects_bad_values(var, value):
    with pytest.raises(ConfigError):
        PerceptionConfig.from_env({var: value})


# ================================================================================ adversarial review round
def test_review_stacked_same_class_objects_confirmed_by_two_detections_stay_separate(make_reconciler, inv):
    """R1: two laptops stacked in one frame (omni boxes IoU >= 0.5). The detector sees two distinct laptops, so the
    same-frame listings are two objects, not a repeat."""
    a, b = (0.10, 0.10, 0.40, 0.40), (0.14, 0.12, 0.44, 0.42)  # IoU ~ 0.6
    frames = [[("laptop-A", a), ("laptop-B", b)]]
    process(make_reconciler(), frames, [obj("laptop", a), obj("laptop", b)], LABELS)
    assert _n_assignments(inv) == 2


def test_review_same_frame_repeat_with_one_detection_is_still_one(make_reconciler, inv):
    a, b = (0.10, 0.10, 0.40, 0.40), (0.11, 0.10, 0.41, 0.40)
    process(make_reconciler(), [[("laptop-A", a)]], [obj("laptop", a), obj("laptop", b)], LABELS)
    assert _n_assignments(inv) == 1


def test_review_ambiguous_relaxed_key_touching_a_private_zone_is_private():
    """R2: a relaxed key shared by a public module and a private zone resolves to nothing, but must still make the
    window private (never less private)."""
    locs = [{"location_id": "SEG", "parent_id": None, "kind": "segment", "display_name": "Segment",
             "no_capture": False},
            {"location_id": "BRV", "parent_id": "SEG", "kind": "module", "display_name": "Bravo Module",
             "no_capture": False},
            {"location_id": "BRVQ", "parent_id": "BRV", "kind": "zone", "display_name": "Bravo Laboratory",
             "no_capture": True}]
    r = LocationResolver(locs)
    assert r.resolve("bravo") is None
    assert r.is_private_hint("the bravo module") is True
    assert r.window_scope("SEG", "bravo") == ("SEG", True)


@pytest.mark.parametrize("camera_id", ["cam-handheld", "cam-lab1-fwd"])
def test_review_scene_summary_naming_a_private_place_makes_the_window_private(make_reconciler, inv, storage,
                                                                               camera_id):
    """R3: the hint is null or names the parent module, but omni's scene summary says crew quarters."""
    rec = make_reconciler(camera_id=camera_id)
    w = scene_window([[("laptop-A", BOX_A)]])
    omni = OmniResult(status="ok", model="m", raw="{}", observation=observation(
        [obj("laptop", BOX_A)], location_hint="Node 2",
        scene_summary="A crew member floats inside a crew quarters sleeping bag."))
    rec.process(w, omni, detections_for([[("laptop-A", BOX_A)]], {"laptop-A": "laptop"}))
    assert storage.objects == {}
    assert inv.last["window"].get("raw_uri") is None


def test_review_detector_linked_sighting_adopts_exemplar_less_entity(make_reconciler, inv):
    """R4: windows with omni-only boxes create an entity without exemplar; when the detector later localises the
    object, it must attach to that entity (and give it its first exemplar), not create a duplicate."""
    rec = make_reconciler(camera_id="cam-handheld")
    for k in range(6):
        t0 = T0 + timedelta(seconds=4 * k)
        if k % 2:
            w, dets = hires_window(TOUR_FRAMES, t0=t0, index=k), detections_for(TOUR_FRAMES, TOUR_LABELS)
        else:
            w, dets = scene_window([[]] * 6, t0=t0, index=k), [[] for _ in range(6)]
        assert rec.process(w, real_omni_result(), dets).status == "posted"
    assert live_counts(inv) == {"laptop": 1, "headset": 1}, live_counts(inv)
    laptop = next(e for e in inv.live() if e["catalog_id"] == "laptop")
    assert len(laptop["embeddings"]) >= 1


def test_review_adoption_never_takes_an_entity_already_judged_different_by_appearance(make_reconciler, inv):
    """R4 guard: an entity that came back as a candidate (it has an exemplar) and scored 'new' is not adopted."""
    other = random_vec(3)
    inv.add_entity(catalog_id="laptop", embedding=other, last_location="LAB1", last_seen_at=T0)
    process(make_reconciler(), [[("laptop-A", BOX_A)]], [obj("laptop", BOX_A)], {"laptop-A": "laptop"})
    a = inv.last["assignments"][0]
    assert a["entity_id"] is None


def random_vec(seed):
    v = np.random.default_rng(seed).normal(size=768)
    return v / np.linalg.norm(v)
