"""AC-PER-5 detector interface (OWLv2 + deterministic fake, min_score 0.15) and AC-PER-6 embedder interface
(DINOv2-base + deterministic fake, 768-d L2-normalised, same crop -> same vector)."""
from __future__ import annotations

import subprocess
import sys

import numpy as np
import pytest

from perception.detector import DEFAULT_MIN_SCORE, OwlV2Detector, filter_detections
from perception.embedder import EMBEDDING_DIM, Dinov2Embedder
from perception.fakes import FakeDetector, FakeEmbedder, make_frame, object_color
from perception.types import Detection

BOX_A = (0.1, 0.1, 0.3, 0.4)
BOX_B = (0.5, 0.5, 0.9, 0.8)


# ------------------------------------------------------------------------------------------------ AC-PER-5
def test_ac_per_05_default_min_score_is_0_15():
    assert DEFAULT_MIN_SCORE == 0.15
    assert OwlV2Detector().min_score == 0.15
    assert FakeDetector().min_score == 0.15


def test_ac_per_05_filter_drops_below_min_score_keeps_boundary():
    dets = [Detection("drill", BOX_A, 0.10), Detection("drill", BOX_A, 0.15), Detection("laptop", BOX_B, 0.9),
            Detection("x", BOX_B, 0.1499)]
    kept = filter_detections(dets)
    assert [d.score for d in kept] == [0.15, 0.9]
    assert filter_detections(dets, min_score=0.5) == [dets[2]]
    assert filter_detections([]) == []


def test_ac_per_05_fake_detector_scripted_per_call_and_filtered():
    script = [[Detection("drill", BOX_A, 0.9), Detection("noise", BOX_B, 0.05)], [Detection("laptop", BOX_B, 0.7)]]
    det = FakeDetector(script=script)
    frame = np.zeros((10, 10, 3), dtype=np.uint8)
    assert det.detect(frame, ["drill"]) == [Detection("drill", BOX_A, 0.9)]
    assert det.detect(frame, ["drill"]) == [Detection("laptop", BOX_B, 0.7)]
    assert det.detect(frame, ["drill"]) == []
    assert len(det.calls) == 3


def test_ac_per_05_fake_detector_callable_script():
    det = FakeDetector(script=lambda frame, labels: [Detection(lbl, BOX_A, 0.5) for lbl in labels])
    out = det.detect(np.zeros((4, 4, 3), dtype=np.uint8), ["drill", "wrench"])
    assert [d.label for d in out] == ["drill", "wrench"]


def test_ac_per_05_fake_detector_finds_painted_objects_by_colour():
    frame = make_frame([("drill-1", BOX_A), ("laptop-1", BOX_B)])
    det = FakeDetector({"drill-1": "drill", "laptop-1": "laptop", "absent": "wrench"})
    out = sorted(det.detect(frame, ["drill", "laptop", "wrench"]), key=lambda d: d.label)
    assert [d.label for d in out] == ["drill", "laptop"]
    for d, box in zip(out, [BOX_A, BOX_B], strict=True):
        assert all(abs(x - y) < 0.01 for x, y in zip(d.bbox, box, strict=True)), (d.bbox, box)
        assert d.score == 0.9


def test_ac_per_05_fake_detector_is_deterministic():
    frame = make_frame([("drill-1", BOX_A)])
    a = FakeDetector({"drill-1": "drill"}).detect(frame, ["drill"])
    b = FakeDetector({"drill-1": "drill"}).detect(frame, ["drill"])
    assert a == b and len(a) == 1


def test_ac_per_05_fake_detector_low_score_objects_dropped():
    frame = make_frame([("drill-1", BOX_A)])
    assert FakeDetector({"drill-1": "drill"}, score=0.1).detect(frame, ["drill"]) == []


def test_ac_per_05_detection_bboxes_are_normalised():
    frame = make_frame([("drill-1", BOX_A)], height=300, width=500)
    for d in FakeDetector({"drill-1": "drill"}).detect(frame, ["drill"]):
        assert all(0.0 <= v <= 1.0 for v in d.bbox)
        assert d.bbox[0] < d.bbox[2] and d.bbox[1] < d.bbox[3]


def test_ac_per_05_06_model_modules_import_lazily():
    code = ("import sys, perception, perception.detector, perception.embedder, perception.replay, perception.fakes; "
            "bad = [m for m in ('torch', 'transformers') if m in sys.modules]; "
            "OwlV2Detector = perception.detector.OwlV2Detector(); perception.embedder.Dinov2Embedder(); "
            "bad += [m for m in ('torch', 'transformers') if m in sys.modules]; print(bad); sys.exit(1 if bad else 0)")
    proc = subprocess.run([sys.executable, "-c", code], check=False, capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, proc.stdout + proc.stderr


def test_ac_per_05_make_frame_paints_object_colours():
    frame = make_frame([("drill-1", BOX_A)], height=100, width=100)
    assert frame.shape == (100, 100, 3) and frame.dtype == np.uint8
    assert tuple(frame[25, 20]) == object_color("drill-1")
    assert tuple(frame[90, 90]) != object_color("drill-1")
    assert object_color("drill-1") == object_color("drill-1") != object_color("drill-2")


@pytest.mark.model
def test_ac_per_05_owlv2_real_model_returns_filtered_detections():
    frame = make_frame([("drill-1", BOX_A)])
    dets = OwlV2Detector().detect(frame, ["a red box", "a drill"])
    assert isinstance(dets, list)
    assert all(isinstance(d, Detection) and d.score >= 0.15 for d in dets)
    assert all(0.0 <= v <= 1.0 for d in dets for v in d.bbox)


# ------------------------------------------------------------------------------------------------ AC-PER-6
def _patch(color, h=20, w=30):
    return np.full((h, w, 3), color, dtype=np.uint8)


def test_ac_per_06_fake_embedding_is_768d_unit_float32():
    v = FakeEmbedder().embed(_patch((200, 10, 10)))
    assert EMBEDDING_DIM == 768
    assert v.shape == (768,) and v.dtype == np.float32
    assert abs(float(np.linalg.norm(v)) - 1.0) < 1e-5


def test_ac_per_06_same_crop_same_vector_across_instances():
    crop = _patch((10, 200, 10))
    a = FakeEmbedder().embed(crop)
    b = FakeEmbedder().embed(crop.copy())
    assert np.array_equal(a, b)


def test_ac_per_06_fake_is_deterministic_per_object_id_regardless_of_crop_size():
    e = FakeEmbedder()
    col = object_color("drill-1")
    v1 = e.embed(_patch(col, 20, 30))
    v2 = e.embed(_patch(col, 60, 45))
    assert np.allclose(v1, v2)
    assert np.allclose(v1, e.vector_for_object("drill-1"))


def test_ac_per_06_fake_tolerates_background_margin_in_crop():
    e = FakeEmbedder()
    frame = make_frame([("drill-1", (0.2, 0.2, 0.6, 0.6))], height=100, width=100)
    crop = frame[15:65, 15:65]  # bbox plus a background margin
    assert np.allclose(e.embed(crop), e.vector_for_object("drill-1"))


def test_ac_per_06_different_objects_are_far_apart():
    e = FakeEmbedder()
    sims = [float(np.dot(e.vector_for_object(f"obj-{i}"), e.vector_for_object(f"obj-{i + 1}"))) for i in range(5)]
    assert all(abs(s) < 0.3 for s in sims), sims


def test_ac_per_06_fake_vector_stable_across_processes():
    code = ("from perception.fakes import FakeEmbedder; "
            "print(','.join(f'{x:.6f}' for x in FakeEmbedder().vector_for_object('drill-1')[:5]))")
    outs = {subprocess.run([sys.executable, "-c", code], check=False, capture_output=True, text=True, timeout=60,
                           env={"PYTHONHASHSEED": seed, "PATH": ""}).stdout for seed in ("1", "2")}
    assert len(outs) == 1 and next(iter(outs)).strip(), outs


@pytest.mark.model
def test_ac_per_06_dinov2_real_model_same_crop_same_vector():
    crop = make_frame([("drill-1", (0.0, 0.0, 1.0, 1.0))], height=64, width=64)
    emb = Dinov2Embedder()
    a, b = emb.embed(crop), emb.embed(crop.copy())
    assert a.shape == (768,)
    assert abs(float(np.linalg.norm(a)) - 1.0) < 1e-4
    assert np.allclose(a, b, atol=1e-5)


# Regression from cluster integration (2026-10-17): transformers 5.x removed post_process_object_detection from
# Owlv2Processor (only the image processor keeps it), so every real detection raised AttributeError.
class _Results(dict):
    pass


class _ImageProcessorV5:
    def __init__(self):
        self.calls = []

    def post_process_object_detection(self, outputs, threshold, target_sizes):
        self.calls.append((threshold, target_sizes))
        return [{"scores": _T([0.9]), "labels": _T([0]), "boxes": _T([[10.0, 20.0, 110.0, 220.0]])}]


class _T(list):
    def tolist(self):
        return list(self)


class _ProcessorV5:
    """transformers 5.x layout: no post_process_object_detection on the processor itself."""

    def __init__(self):
        self.image_processor = _ImageProcessorV5()

    def post_process_grounded_object_detection(self, *a, **k):  # pragma: no cover - must not be preferred
        raise AssertionError("grounded variant used although image_processor supports post_process_object_detection")


def test_ac_per_05_post_process_supports_transformers_v5_layout():
    from perception.detector import OwlV2Detector

    det = OwlV2Detector(min_score=0.2)
    det._processor = _ProcessorV5()
    out = det._post_process(object(), target_sizes=[[400, 400]])
    assert out["scores"].tolist() == [0.9]
    assert det._processor.image_processor.calls == [(0.2, [[400, 400]])]
