"""AC-PER-54 detector boxes for unknowns, AC-PER-55 query boxes and people, AC-PER-56 review image of an unknown,
AC-PER-57 tests.

Live (Gemma 4 run, 2026-10-18): the unknowns "orange tool", "orange sphere" and "white wrapped object" were stored with
no box (OWLv2 was only asked for catalog names; Gemma's boxes were on a 0..1000 scale and nulled), so their reviews
showed a whole 1080x608 frame and the operator had to hunt for the object. OWLv2 is open-vocabulary: the unknown's own
label becomes an extra text query on its frame."""
from __future__ import annotations

import json
import os
import subprocess
from datetime import timedelta
from pathlib import Path

import numpy as np
import pytest
from support import BUCKET, CAMERA_LOCATIONS, T0, FakeInventory, obj, observation, scene_window

import perception.replay
from perception.config import ConfigError, PerceptionConfig
from perception.fakes import FakeDecision, FakeDetector, FakeEmbedder, FakeOmni, MemoryStorage
from perception.types import Detection, OmniResult

FIXTURES = Path(__file__).resolve().parent / "fixtures"
LIVE = FIXTURES / "gemma4_unknowns_2026-10-18.json"
FRAME_W, FRAME_H = 1080, 608


def px(x1, y1, x2, y2):
    return (x1 / FRAME_W, y1 / FRAME_H, x2 / FRAME_W, y2 / FRAME_H)


SPHERE = px(565, 255, 605, 292)  # the orange sphere on the live Kibo frame
SPHERE_2 = px(700, 400, 760, 450)  # a lower-scoring second "orange sphere" box, clear of everyone
PERSON = px(450, 120, 760, 600)  # a crew member around the sphere
LAPTOP = px(100, 400, 300, 520)


class QueryDetector:
    """Fake extra detector: ``boxes`` = {query text: [(frame index, bbox, score), ...]}; records every call."""

    def __init__(self, boxes=None, *, fail=False):
        self.boxes = boxes or {}
        self.fail = fail
        self.calls: list[tuple[int, list[str]]] = []

    def __call__(self, frame_idx, labels):
        self.calls.append((frame_idx, list(labels)))
        if self.fail:
            raise RuntimeError("detector down")
        return [Detection(q, tuple(b), s) for q in labels for (f, b, s) in self.boxes.get(q, []) if f == frame_idx]


def run(rec, objects, *, qd=None, dets=None, n_frames=1, people=(), summary="crew working", hint=None, t=0.0):
    w = scene_window([[] for _ in range(n_frames)], t0=T0 + timedelta(seconds=t), height=FRAME_H, width=FRAME_W)
    omni = OmniResult(status="ok", observation=observation(objects, people=people, scene_summary=summary,
                                                           location_hint=hint), model="fake-omni", raw="{}")
    kwargs = {} if qd is None else {"query_detect": qd}
    out = rec.process(w, omni, dets if dets is not None else [[] for _ in range(n_frames)], **kwargs)
    assert out.status == "posted", out.error
    return out.payload


def by_label(payload, label):
    return [a for a in payload["assignments"] if (a.get("new_entity") or {}).get("label") == label]


def review_for(payload, assignment):
    return [r for r in payload["reviews"] if r["entity_ref"] == assignment["ref"]]


def crop_keys(storage):
    return [k for k in storage.objects if k.startswith("crops/")]


# ------------------------------------------------------------------------------------------------ AC-PER-54 link
def test_ac_per_54_unknown_gets_the_query_box_as_detector_box(make_reconciler, storage):
    rec = make_reconciler()
    qd = QueryDetector({"orange sphere": [(0, SPHERE, 0.45)]})
    p = run(rec, [obj("orange sphere", None, description="a small orange spherical object")], qd=qd)
    [a] = by_label(p, "orange sphere")
    assert a["sighting"]["bbox"] == pytest.approx(list(SPHERE), abs=1e-6)
    assert a["sighting"]["bbox_source"] == "detector"
    assert a["exemplar"] is not None and a["exemplar"]["crop_uri"]  # the existing pipeline: crop + exemplar
    assert len(crop_keys(storage)) == 1
    assert qd.calls == [(0, ["orange sphere"])]
    assert p["window"]["unknown_queries"] == 1 and p["window"]["unknown_boxes_linked"] == 1


def test_ac_per_54_query_runs_on_the_objects_frame_index(make_reconciler):
    rec = make_reconciler()
    qd = QueryDetector({"orange sphere": [(3, SPHERE, 0.45), (0, SPHERE_2, 0.9)]})
    p = run(rec, [obj("orange sphere", None, frame_index=3)], qd=qd, n_frames=6)
    [a] = by_label(p, "orange sphere")
    assert qd.calls == [(3, ["orange sphere"])]
    assert a["sighting"]["bbox"] == pytest.approx(list(SPHERE), abs=1e-6)
    assert a["sighting"]["seen_at"] == (T0 + timedelta(seconds=3)).isoformat()


def test_ac_per_54_no_frame_index_uses_the_latest_frame(make_reconciler):
    rec = make_reconciler()
    qd = QueryDetector({"orange sphere": [(5, SPHERE, 0.45)]})
    p = run(rec, [obj("orange sphere", None, frame_index=None)], qd=qd, n_frames=6)
    assert qd.calls == [(5, ["orange sphere"])]
    assert by_label(p, "orange sphere")[0]["sighting"]["bbox_source"] == "detector"


def test_ac_per_54_best_scoring_box_is_linked(make_reconciler):
    rec = make_reconciler()
    qd = QueryDetector({"orange sphere": [(0, SPHERE_2, 0.3), (0, SPHERE, 0.6)]})
    p = run(rec, [obj("orange sphere")], qd=qd)
    assert by_label(p, "orange sphere")[0]["sighting"]["bbox"] == pytest.approx(list(SPHERE), abs=1e-6)


def test_ac_per_54_query_text_is_the_normalised_label(make_reconciler):
    rec = make_reconciler()
    qd = QueryDetector({"orange sphere": [(0, SPHERE, 0.45)]})
    run(rec, [obj("  Orange   Sphere! ")], qd=qd)
    assert qd.calls == [(0, ["orange sphere"])]


def test_ac_per_54_box_for_another_query_is_not_linked(make_reconciler):
    rec = make_reconciler()
    qd = QueryDetector({"orange tool": [(0, SPHERE, 0.9)]})  # the detector answers a different text
    p = run(rec, [obj("orange sphere")], qd=qd)
    assert by_label(p, "orange sphere")[0]["sighting"]["bbox"] is None


# ------------------------------------------------------------------------------------------------ threshold
@pytest.mark.parametrize("score,linked", [(0.29, False), (0.3, True), (0.6, True)])
def test_ac_per_54_link_threshold_default(make_reconciler, score, linked):
    rec = make_reconciler()
    p = run(rec, [obj("orange sphere")], qd=QueryDetector({"orange sphere": [(0, SPHERE, score)]}))
    s = by_label(p, "orange sphere")[0]["sighting"]
    assert (s["bbox_source"] == "detector") is linked
    if not linked:
        assert s["bbox"] is None and p["window"]["unknown_boxes_linked"] == 0


def test_ac_per_54_link_threshold_configurable(make_reconciler):
    rec = make_reconciler(unknown_query_min_score=0.5)
    p = run(rec, [obj("orange sphere")], qd=QueryDetector({"orange sphere": [(0, SPHERE, 0.45)]}))
    assert by_label(p, "orange sphere")[0]["sighting"]["bbox"] is None


def test_ac_per_54_below_threshold_keeps_omnis_box(make_reconciler):
    rec = make_reconciler()
    omni_box = (0.5, 0.4, 0.6, 0.5)
    p = run(rec, [obj("orange sphere", omni_box)], qd=QueryDetector({"orange sphere": [(0, SPHERE, 0.1)]}))
    s = by_label(p, "orange sphere")[0]["sighting"]
    assert s["bbox"] == pytest.approx(list(omni_box)) and s["bbox_source"] == "omni"


def test_ac_per_54_config_from_env():
    c = PerceptionConfig.from_env({"PERCEPTION_UNKNOWN_QUERIES_MAX": "3", "PERCEPTION_UNKNOWN_QUERY_MIN_SCORE": "0.3"})
    assert c.unknown_queries_max == 3 and c.unknown_query_min_score == pytest.approx(0.3)
    d = PerceptionConfig()
    assert d.unknown_queries_max == 6 and d.unknown_query_min_score == pytest.approx(0.3)
    with pytest.raises(ConfigError):
        PerceptionConfig.from_env({"PERCEPTION_UNKNOWN_QUERY_MIN_SCORE": "1.5"})
    with pytest.raises(ConfigError):
        PerceptionConfig.from_env({"PERCEPTION_UNKNOWN_QUERIES_MAX": "41"})


# ------------------------------------------------------------------------------------------------ budget
def test_ac_per_54_budget_caps_distinct_queries_by_confidence(make_reconciler):
    rec = make_reconciler()
    labels = [f"strange gadget {c}" for c in "abcdefgh"]
    objects = [obj(lbl, None, confidence=0.5 + i * 0.05) for i, lbl in enumerate(labels)]  # h is the most confident
    qd = QueryDetector()
    p = run(rec, objects, qd=qd)
    asked = [q for _, qs in qd.calls for q in qs]
    assert len(asked) == 6 and set(asked) == set(labels[2:])
    assert len(qd.calls) == 1  # one frame -> one extra call
    assert p["window"]["unknown_queries"] == 6


def test_ac_per_54_budget_configurable_and_zero_is_off(make_reconciler):
    rec = make_reconciler(unknown_queries_max=0)
    qd = QueryDetector({"orange sphere": [(0, SPHERE, 0.9)]})
    p = run(rec, [obj("orange sphere")], qd=qd)
    assert qd.calls == [] and by_label(p, "orange sphere")[0]["sighting"]["bbox"] is None
    rec = make_reconciler(unknown_queries_max=2)
    qd = QueryDetector()
    run(rec, [obj("thing a"), obj("thing b"), obj("thing c")], qd=qd, t=100)
    assert sum(len(qs) for _, qs in qd.calls) == 2


def test_ac_per_54_one_call_per_frame_with_queries(make_reconciler):
    rec = make_reconciler()
    qd = QueryDetector()
    run(rec, [obj("orange sphere", None, frame_index=0), obj("orange tool", None, frame_index=2),
              obj("white wrapped object", None, frame_index=0)], qd=qd, n_frames=6)
    assert sorted((f, sorted(qs)) for f, qs in qd.calls) == [(0, ["orange sphere", "white wrapped object"]),
                                                             (2, ["orange tool"])]


def test_ac_per_54_same_label_shares_one_query(make_reconciler):
    rec = make_reconciler()
    qd = QueryDetector({"orange sphere": [(0, SPHERE, 0.6), (0, SPHERE_2, 0.5)]})
    # two boxless listings of one label in the same frame stay two instances (AC-PER-19)
    p = run(rec, [obj("orange sphere"), obj("orange sphere", confidence=0.8)], qd=qd)
    assert qd.calls == [(0, ["orange sphere"])]
    boxes = sorted(tuple(a["sighting"]["bbox"]) for a in by_label(p, "orange sphere"))
    assert len(boxes) == 2 and boxes[0] != boxes[1]  # the second instance takes the next unclaimed box


# ------------------------------------------------------------------------------------------------ who queries
@pytest.mark.parametrize("label", ["laptop", "hatch", "window", "hand", "the man", "face"])
def test_ac_per_54_no_query_for_catalog_structure_or_body_labels(make_reconciler, label):
    rec = make_reconciler()
    qd = QueryDetector()
    run(rec, [obj(label, LAPTOP)], qd=qd)
    assert qd.calls == []


@pytest.mark.parametrize("label", ["astronaut's bag", "crew member glove", "person-shaped toy", "man's watch",
                                   "cosmonaut kit", "people mover", "crewman's pouch", "kid's toy", "hair clip",
                                   "spacewalker tether"])
def test_ac_per_55_person_word_labels_are_never_queries(make_reconciler, label):
    rec = make_reconciler()
    qd = QueryDetector()
    run(rec, [obj(label)], qd=qd)
    assert qd.calls == []


@pytest.mark.parametrize("text,expected", [
    ("astronaut's bag", True), ("crew member glove", True), ("person", True), ("human hand", True),
    ("cosmonaut kit", True), ("woman", True), ("faces", True),
    ("orange sphere", False), ("white wrapped object", False), ("manual", False), ("crewlock bag", False),
    ("headset", False), ("handle bag", False),
    ("crewmen kit", True), ("spacewalker tether", True), ("child seat", True), ("someone's cup", True),
    ("eye drops", True), ("neck pillow", True), ("girl doll", True), ("guy wire", True), ("ASTRONAUT’S pen", True),
])
def test_ac_per_55_mentions_person(text, expected):
    from perception.privacy import mentions_person

    assert mentions_person(text) is expected


def test_ac_per_54_no_queries_in_a_private_window(make_reconciler):
    rec = make_reconciler(camera_id="cam-unmapped")  # no camera location -> no_capture (fail closed)
    qd = QueryDetector({"orange sphere": [(0, SPHERE, 0.9)]})
    p = run(rec, [obj("orange sphere")], qd=qd)
    assert qd.calls == [] and p["window"]["private"] is True


def test_ac_per_54_already_linked_unknown_does_not_query(make_reconciler):
    rec = make_reconciler()
    qd = QueryDetector()
    # an unknown whose omni box agrees with a detector box (AC-PER-8 pass 1) is already localised
    dets = [[Detection("laptop", LAPTOP, 0.9)]]
    run(rec, [obj("strange gadget", LAPTOP)], qd=qd, dets=dets)
    assert qd.calls == []


def test_ac_per_54_query_failure_still_writes_the_window(make_reconciler):
    rec = make_reconciler()
    p = run(rec, [obj("orange sphere")], qd=QueryDetector(fail=True))
    assert by_label(p, "orange sphere")[0]["sighting"]["bbox"] is None


def test_ac_per_54_no_query_detector_is_the_old_behaviour(make_reconciler):
    rec = make_reconciler()
    p = run(rec, [obj("orange sphere")])
    assert by_label(p, "orange sphere")[0]["sighting"]["bbox"] is None


# ------------------------------------------------------------------------------------------------ AC-PER-55 privacy
def test_ac_per_55_query_box_over_a_person_gets_no_crop(make_reconciler, storage):
    rec = make_reconciler()
    qd = QueryDetector({"orange sphere": [(0, SPHERE, 0.8), (0, SPHERE_2, 0.4)]})
    dets = [[Detection("person", PERSON, 0.9)]]
    p = run(rec, [obj("orange sphere")], qd=qd, dets=dets)
    [a] = by_label(p, "orange sphere")
    assert crop_keys(storage) == []
    assert a["exemplar"] is None
    assert a["sighting"]["bbox"] is None  # never linked; and the lower clear box is not used instead
    assert p["window"]["unknown_query_person_rejected"] == 1 and p["window"]["unknown_boxes_linked"] == 0
    [r] = review_for(p, a)
    assert r["image_uri"] == a["sighting"]["frame_uri"]


def test_ac_per_55_face_and_omni_body_regions_also_block(make_reconciler, storage):
    rec = make_reconciler()
    face = px(570, 250, 610, 300)
    qd = QueryDetector({"orange sphere": [(0, SPHERE, 0.8)]})
    run(rec, [obj("orange sphere")], qd=qd, dets=[[Detection("face", face, 0.5)]])
    assert crop_keys(storage) == []
    p = run(rec, [obj("orange sphere"), obj("hand", px(560, 240, 620, 310))], qd=qd, t=100)
    assert crop_keys(storage) == [] and p["window"]["unknown_query_person_rejected"] == 1


def test_ac_per_55_crop_guard_still_runs_on_query_boxes(make_reconciler, storage, monkeypatch):
    """Defence in depth: even if the link check were bypassed, the AC-PER-49 guard drops the crop."""
    rec = make_reconciler()
    monkeypatch.setattr(rec, "_query_box_overlaps_person", lambda *a, **k: False)
    qd = QueryDetector({"orange sphere": [(0, SPHERE, 0.8)]})
    p = run(rec, [obj("orange sphere")], qd=qd, dets=[[Detection("person", PERSON, 0.9)]])
    [a] = by_label(p, "orange sphere")
    assert crop_keys(storage) == [] and a["exemplar"] is None and a["sighting"].get("person_overlap") is True


def test_ac_per_55_query_box_clear_of_people_is_kept(make_reconciler, storage):
    rec = make_reconciler()
    qd = QueryDetector({"orange sphere": [(0, SPHERE, 0.8)]})
    p = run(rec, [obj("orange sphere")], qd=qd, dets=[[Detection("person", px(0, 0, 300, 600), 0.9)]])
    assert len(crop_keys(storage)) == 1 and by_label(p, "orange sphere")[0]["sighting"]["bbox_source"] == "detector"


def test_ac_per_55_queries_never_include_person_or_face(make_reconciler):
    rec = make_reconciler()
    qd = QueryDetector()
    run(rec, [obj("orange sphere"), obj("person"), obj("face")], qd=qd)
    assert all(q not in ("person", "face") for _, qs in qd.calls for q in qs)


# ------------------------------------------------------------------------------------------------ catalog unaffected
def _laptop_payload(make_reconciler, qd):
    rec = make_reconciler()
    dets = [[Detection("laptop", LAPTOP, 0.9)]]
    return run(rec, [obj("laptop", LAPTOP), obj("orange sphere")], qd=qd, dets=dets)


def test_ac_per_54_catalog_objects_unaffected(make_reconciler, inv):
    without = _laptop_payload(make_reconciler, None)
    inv.entities.clear()
    # the query box sits exactly on the laptop: it must neither move the laptop's link nor be claimed twice
    qd = QueryDetector({"orange sphere": [(0, LAPTOP, 0.95), (0, SPHERE, 0.5)]})
    with_q = _laptop_payload(make_reconciler, qd)
    [a0], [a1] = by_label(without, "laptop"), by_label(with_q, "laptop")
    assert a0["sighting"]["bbox"] == a1["sighting"]["bbox"] == pytest.approx(list(LAPTOP), abs=1e-6)
    assert a0["sighting"]["bbox_source"] == a1["sighting"]["bbox_source"] == "detector"
    assert a0["exemplar"]["embedding"] == a1["exemplar"]["embedding"]
    assert qd.calls == [(0, ["orange sphere"])]  # never the catalog labels again
    # the sphere skipped the laptop's (claimed) box and took the next one
    assert by_label(with_q, "orange sphere")[0]["sighting"]["bbox"] == pytest.approx(list(SPHERE), abs=1e-6)


def test_ac_per_54_query_boxes_are_not_person_regions(make_reconciler):
    rec = make_reconciler()
    qd = QueryDetector({"orange sphere": [(0, SPHERE, 0.8)]})
    p = run(rec, [obj("orange sphere"), obj("laptop", LAPTOP)], qd=qd, dets=[[Detection("laptop", LAPTOP, 0.9)]])
    assert all("person_boxes" not in a["sighting"] for a in p["assignments"])


# ------------------------------------------------------------------------------------------------ AC-PER-56 review
def test_ac_per_56_unknown_review_is_the_frame_with_the_box(make_reconciler):
    rec = make_reconciler()
    qd = QueryDetector({"orange sphere": [(0, SPHERE, 0.45)]})
    p = run(rec, [obj("orange sphere")], qd=qd)
    [a] = by_label(p, "orange sphere")
    [r] = review_for(p, a)
    assert r["kind"] == "unknown_object"
    assert r["image_uri"] == a["sighting"]["frame_uri"] != a["exemplar"]["crop_uri"]
    assert a["sighting"]["bbox"] is not None  # inventory-api returns it as image_bbox (frame_uri == image_uri)


def test_ac_per_56_catalog_reviews_keep_the_crop(make_reconciler, inv):
    from support import CATALOG

    rec = make_reconciler(decision=FakeDecision())
    laptop = next(c for c in CATALOG if c["catalog_id"] == "laptop")
    # an expected-count review on a catalog class keeps the crop image (unchanged behaviour)
    for _ in range(int(laptop.get("expected_count") or 1)):
        inv.add_entity(catalog_id="laptop", last_location="NOD2", last_seen_at=T0 - timedelta(hours=1))
    p = run(rec, [obj("laptop", LAPTOP)], dets=[[Detection("laptop", LAPTOP, 0.9)]])
    [a] = by_label(p, "laptop") or [x for x in p["assignments"] if x["exemplar"]]
    assert a["exemplar"] and review_for(p, a), p["reviews"]
    for r in review_for(p, a):
        assert r["image_uri"] == a["exemplar"]["crop_uri"]


def test_ac_per_56_inventory_contract_accepts_the_payload(make_reconciler, inv):
    """FakeInventory validates every posted payload against inventory-api's IngestWindow model."""
    rec = make_reconciler()
    run(rec, [obj("orange sphere")], qd=QueryDetector({"orange sphere": [(0, SPHERE, 0.45)]}))
    assert inv.posted and inv.reviews


# ------------------------------------------------------------------------------------------------ replay, live reply
def _live_window(unknown):
    data = json.loads(LIVE.read_text())
    return next(w for w in data["windows"] if w["unknown"] == unknown)


def _paint(i):
    frame = np.zeros((FRAME_H, FRAME_W, 3), dtype=np.uint8)
    frame[:, :] = (10 * i, 20, 30)
    return frame


@pytest.mark.parametrize("unknown", ["orange sphere", "orange tool", "white wrapped object"])
def test_ac_per_57_replay_live_gemma_reply_links_the_unknown(unknown):
    live = _live_window(unknown)
    reply = live["reply"]
    target = next(o for o in reply["objects"] if o["label"] == unknown)
    fi = target["frame_index"]
    box = (0.40, 0.40, 0.48, 0.50)

    crew = (0.75, 0.1, 0.95, 0.9)  # omni reports people[]: the detector boxes the crew member on frame 4, clear of
    # the unknown (otherwise AC-PER-55 fails closed, tested above)

    def script(frame, labels):
        if list(labels) == [unknown] and int(frame[0, 0, 0]) == 10 * fi:
            return [Detection(unknown, box, 0.42)]
        if "person" in labels and reply["people"] and int(frame[0, 0, 0]) == 40:
            return [Detection("person", crew, 0.8)]
        return []  # the catalog query finds no object (as live: no link)

    detector = FakeDetector(script=script)
    inv = FakeInventory()
    storage = MemoryStorage(BUCKET)
    items = [(T0 + timedelta(seconds=i), _paint(i), None) for i in range(6)]
    config = PerceptionConfig(camera_locations=dict(CAMERA_LOCATIONS), s3_bucket=BUCKET, window_frames=6,
                              stride_s=5.0, detect_long_side=0, long_side=FRAME_W)
    stats = perception.replay.run(items, camera_id="cam-handheld", start_time=T0, api=inv,
                                  omni=FakeOmni([observation([]), json.dumps(reply)]), detector=detector,
                                  embedder=FakeEmbedder(),
                                  decision=FakeDecision(), asr=None, storage=storage, config=config)
    # the first window is emitted on the first frame (AC-PER-2, empty reply); the live reply is the 6-frame window
    assert stats.posted == 2 and len(omni_frames(inv)) == 2
    payload = inv.posted[1]
    [a] = [x for x in payload["assignments"] if (x.get("new_entity") or {}).get("label") == unknown]
    assert a["sighting"]["bbox"] == pytest.approx(list(box), abs=1e-6)
    assert a["sighting"]["bbox_source"] == "detector"
    assert a["exemplar"] and a["exemplar"]["crop_uri"]
    [r] = [x for x in payload["reviews"] if x["entity_ref"] == a["ref"]]
    assert r["kind"] == "unknown_object" and r["image_uri"] == a["sighting"]["frame_uri"]
    # 6 catalog calls (one per frame, cached across windows) + exactly one extra call with only the unknown label
    extra = [labels for _, labels in detector.calls[6:]]
    assert extra == [[unknown]]
    assert all("person" in labels for _, labels in detector.calls[:6])
    # catalog-mapped listings of the same reply are not queried
    assert stats.unknown_queries == 1 and stats.unknown_boxes_linked == 1


def omni_frames(inv):
    return [p["window"] for p in inv.posted]


def test_ac_per_57_live_fixture_shape():
    data = json.loads(LIVE.read_text())
    assert {w["unknown"] for w in data["windows"]} == {"orange sphere", "orange tool", "white wrapped object"}
    for w in data["windows"]:
        assert any(o["label"] == w["unknown"] for o in w["reply"]["objects"])


# ------------------------------------------------------------------------------------------------ real OWLv2
KIBO_FRAME = os.environ.get("PERCEPTION_KIBO_FRAME", "")
TOUR_VIDEO = Path(os.environ.get("PERCEPTION_TOUR_VIDEO", str(Path.home() / "nasa-media/iss-tour/tour-medium.mp4")))
KIBO_T = os.environ.get("PERCEPTION_KIBO_T", "55")  # video seconds of the live Kibo frame (window 06:28:33)


def _kibo_frame(tmp_path):
    from PIL import Image

    if KIBO_FRAME and Path(KIBO_FRAME).exists():
        return np.asarray(Image.open(KIBO_FRAME).convert("RGB"))
    if not (TOUR_VIDEO.exists() and KIBO_T):
        pytest.skip("set PERCEPTION_KIBO_FRAME, or PERCEPTION_KIBO_T with the tour video")
    import imageio_ffmpeg

    out = tmp_path / "kibo.png"
    subprocess.run([imageio_ffmpeg.get_ffmpeg_exe(), "-loglevel", "error", "-y", "-ss", KIBO_T, "-i", str(TOUR_VIDEO),
                    "-frames:v", "1", "-vf", "scale=1080:-2", str(out)], check=True, timeout=60)
    return np.asarray(Image.open(out).convert("RGB"))


@pytest.mark.model
def test_ac_per_57_real_owlv2_finds_the_orange_sphere(tmp_path):
    from perception.detector import OwlV2Detector

    frame = _kibo_frame(tmp_path)
    dets = sorted(OwlV2Detector().detect(frame, ["orange sphere"]), key=lambda d: -d.score)
    assert dets, "OWLv2 found no orange sphere"
    best = dets[0]
    assert best.score >= PerceptionConfig().unknown_query_min_score
    cx, cy = (best.bbox[0] + best.bbox[2]) / 2, (best.bbox[1] + best.bbox[3]) / 2
    assert 0.48 <= cx <= 0.60 and 0.36 <= cy <= 0.54, best  # the sphere above the Kibo hatch


# ------------------------------------------------------------------------------------------------ review round fixes
def test_review_people_reported_but_no_person_region_anywhere_fails_closed(make_reconciler, storage):
    """P2: the detector missed the crew member omni reports (people[] has no box): a free-text query box may sit on
    them, and the crop guard has no region to check against. No query box is linked."""
    rec = make_reconciler()
    qd = QueryDetector({"white wrapped object": [(0, PERSON, 0.8)]})
    people = [{"ref": "P1", "appearance": "crew member in white", "activity": "floating", "is_speaking": False}]
    p = run(rec, [obj("white wrapped object")], qd=qd, people=people)
    [a] = by_label(p, "white wrapped object")
    assert crop_keys(storage) == [] and a["exemplar"] is None and a["sighting"]["bbox"] is None
    assert p["window"]["unknown_query_person_rejected"] == 1


def test_review_people_reported_with_a_person_region_elsewhere_still_links(make_reconciler, storage):
    """The live Kibo window: the person floats in at frame 4 (detector box there); the sphere is queried on frame 0."""
    rec = make_reconciler()
    qd = QueryDetector({"orange sphere": [(0, SPHERE, 0.6)]})
    people = [{"ref": "P1", "appearance": "blue shirt", "activity": "floating", "is_speaking": False}]
    dets = [[] for _ in range(6)]
    dets[4] = [Detection("person", PERSON, 0.9)]
    p = run(rec, [obj("orange sphere", None, frame_index=0)], qd=qd, people=people, dets=dets, n_frames=6)
    assert by_label(p, "orange sphere")[0]["sighting"]["bbox_source"] == "detector"
    assert len(crop_keys(storage)) == 1


def test_review_person_object_without_region_fails_closed(make_reconciler, storage):
    rec = make_reconciler()
    qd = QueryDetector({"orange sphere": [(0, SPHERE, 0.6)]})
    p = run(rec, [obj("orange sphere"), obj("person", None)], qd=qd)
    assert crop_keys(storage) == [] and by_label(p, "orange sphere")[0]["sighting"]["bbox"] is None


def test_review_query_box_on_an_unlinked_catalog_detection_is_skipped(make_reconciler):
    """P2: the laptop is linked on frame 1; its unlinked detection on frame 0 is still the laptop's box."""
    rec = make_reconciler()
    dets = [[Detection("laptop", LAPTOP, 0.9)], [Detection("laptop", LAPTOP, 0.9)]]
    qd = QueryDetector({"orange tool": [(0, LAPTOP, 0.6), (0, SPHERE, 0.4)]})
    p = run(rec, [obj("laptop", LAPTOP, frame_index=1), obj("orange tool", None, frame_index=0)], qd=qd, dets=dets,
            n_frames=2)
    assert by_label(p, "orange tool")[0]["sighting"]["bbox"] == pytest.approx(list(SPHERE), abs=1e-6)


@pytest.mark.parametrize("label,query", [
    ("orange tool on the wall", "orange tool"),
    ("small white bag attached to the rack near the hatch", "small white bag"),
    ("a very long strange twisted orange metallic cylindrical gadget thing", "twisted orange metallic cylindrical gadget thing"),
])
def test_review_query_is_the_short_noun_phrase(make_reconciler, label, query):
    """P2: OWLv2 text queries are capped at 16 tokens; a long label in a batched call must not fail the frame."""
    rec = make_reconciler()
    qd = QueryDetector()
    run(rec, [obj(label)], qd=qd)
    assert qd.calls == [(0, [query])]


def test_review_failed_call_counts_no_queries(make_reconciler):
    rec = make_reconciler()
    p = run(rec, [obj("orange sphere")], qd=QueryDetector(fail=True))
    assert p["window"]["unknown_queries"] == 0


def test_review_replay_stats_count_person_rejections():
    from perception.types import ReplayStats

    assert ReplayStats().unknown_query_person_rejected == 0


def test_review_catalog_crop_fallback_with_person_overlap_still_uses_the_frame(make_reconciler, storage):
    """AC-PER-49 for a catalog object (the AC-PER-49 tests use an unknown, which AC-PER-56 now always frames)."""
    rec = make_reconciler()
    from support import CATALOG

    laptop = next(c for c in CATALOG if c["catalog_id"] == "laptop")
    for _ in range(int(laptop.get("expected_count") or 1)):
        rec.api.add_entity(catalog_id="laptop", last_location="NOD2", last_seen_at=T0 - timedelta(hours=1))
    box = px(450, 150, 700, 400)  # over the person's head zone
    p = run(rec, [obj("laptop", box)], dets=[[Detection("laptop", box, 0.9), Detection("person", PERSON, 0.9)]])
    [a] = [x for x in p["assignments"] if x["sighting"].get("person_overlap")]
    assert crop_keys(storage) == [] and a["exemplar"] is None
    rs = review_for(p, a)
    assert rs and all(r["image_uri"] == a["sighting"]["frame_uri"] for r in rs)
