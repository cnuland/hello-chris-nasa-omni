"""AC-EVAL-5 (review round): the runner builds perception's collaborators from the target.

- ``local``: deterministic fakes scripted from the gold data (GoldDetector finds the gold boxes on the frame it is
  given, also after perception's downscale; GoldOmni answers a window with the gold objects / events / people /
  transcript of its frames; FakeASR returns the scripted transcript lines by audio time).
- ``cluster``: perception's real clients configured from the environment (OMNI_URL, DECISION_URL, ASR_URL).
- The inventory-api is bound in-process to the fresh database (no external EVAL_INVENTORY_API_URL).

``perception`` is put on sys.path by the ``perception_on_path`` fixture (only its light modules are used).
"""
from __future__ import annotations

import importlib.util
import json
from datetime import timedelta

import pytest
from conftest import DB_PREFIX

from nasa_eval.dataset import load_dataset
from nasa_eval.runner import ConfigError


@pytest.fixture(scope="module")
def synth_ds(tmp_path_factory):
    from nasa_eval.synth import generate_scene, write_scene

    return load_dataset(write_scene(generate_scene(7), tmp_path_factory.mktemp("s"), "synth-7"))


def _frames(ds):
    import numpy as np
    from PIL import Image

    return [np.asarray(Image.open(p).convert("RGB")) for p in ds.frame_paths()]


def _gold_at(ds, i):
    t = i / ds.fps
    return {(g.gold_object_id, a.bbox) for g in ds.gold for a in g.appearances if abs(a.t - t) < 1e-6}


def test_ac_eval_05_catalog_labels_from_seed():
    from nasa_eval.collaborators import catalog_labels

    labels = catalog_labels()
    assert labels["drill-pistol"] == "drill" and labels["camera-dslr"] == "camera"


@pytest.mark.parametrize("downscale", [False, True])
def test_ac_eval_05_gold_detector_finds_gold_boxes(synth_ds, perception_on_path, downscale):
    from perception.media import downscale as per_downscale
    from perception.types import Detection

    from nasa_eval.collaborators import GoldDetector, catalog_labels

    labels = catalog_labels()
    det = GoldDetector(synth_ds, labels)
    frames = _frames(synth_ds)
    by_id = {g.gold_object_id: g for g in synth_ds.gold}
    for i in (0, 15, 30, 59):
        frame = per_downscale(frames[i], 448) if downscale else frames[i]
        got = det.detect(frame, sorted(set(labels.values())))
        assert all(isinstance(d, Detection) for d in got)
        expected = {(labels[by_id[gid].catalog_id], bbox) for gid, bbox in _gold_at(synth_ds, i)
                    if not by_id[gid].is_unknown}
        assert {(d.label, d.bbox) for d in got} == expected, i


def _window(ds, frames, first, n=6, index=0):
    from perception.types import Keyframe, Window

    kfs = [Keyframe(ds.manifest.start_time + timedelta(seconds=k / ds.fps), frames[k], pts=k / ds.fps, index=k)
           for k in range(first, min(first + n, len(frames)))]
    return Window(kfs[0].timestamp, kfs[-1].timestamp, kfs, None, index)


def test_ac_eval_05_gold_omni_answers_windows_from_gold(synth_ds, perception_on_path):
    from perception.schema import validate_observation

    from nasa_eval.collaborators import GoldOmni, catalog_labels

    labels = catalog_labels()
    omni = GoldOmni(synth_ds, labels)
    frames = _frames(synth_ds)
    mover = next(g for g in synth_ds.gold if any(e.type == "moved" for e in g.events))
    move = next(e for e in mover.events if e.type == "moved")
    events, transcripts = [], []
    for w, first in enumerate(range(0, len(frames), 4)):           # 6-frame windows every 4 s (AC-PER-2)
        res = omni.analyze(_window(synth_ds, frames, first, index=w))
        assert res.ok and validate_observation(res.observation) == [], res.error
        obs = res.observation
        events += obs["events"]
        transcripts.append(obs["transcript"])
        if first == 0:
            labels_seen = {o["label"] for o in obs["objects"]}
            assert labels[mover.catalog_id] in labels_seen
            assert all(0 <= o["frame_index"] < 6 for o in obs["objects"])
        for ev in obs["events"]:
            refs = {p["ref"]: p["appearance"] for p in obs["people"]}
            assert ev["actor_ref"] in refs
    moved = [e for e in events if e["type"] == "moved"]
    assert len(moved) == 1, "an event spanning two overlapping windows is reported once"
    assert moved[0]["object_label"] == labels[mover.catalog_id] and moved[0]["to_location_hint"] == move.to_location
    assert sum(1 for e in events if e["type"] == "removed") == 1
    spoken = " ".join(transcripts)
    assert all(line.text in spoken for line in synth_ds.transcript_lines())


def test_ac_eval_05_gold_omni_unknown_object_gets_a_non_catalog_label(synth_ds, perception_on_path):
    from nasa_eval.collaborators import GoldOmni, catalog_labels

    labels = catalog_labels()
    unk = next(g for g in synth_ds.gold if g.is_unknown)
    first = round(unk.appearances[0].t * synth_ds.fps)
    obs = GoldOmni(synth_ds, labels).analyze(_window(synth_ds, _frames(synth_ds), first)).observation
    unknown_labels = {o["label"] for o in obs["objects"]} - set(labels.values())
    assert len(unknown_labels) == 1


def test_ac_eval_05_gold_asr_returns_lines_by_audio_time(synth_ds, perception_on_path):
    import numpy as np
    from perception.types import AudioChunk

    from nasa_eval.collaborators import gold_asr

    asr = gold_asr(synth_ds)
    line = synth_ds.transcript_lines()[1]
    t0 = synth_ds.manifest.start_time + timedelta(seconds=line.t)
    chunk = AudioChunk(t0, np.zeros(16000 * 5, dtype=np.int16))
    assert line.text in asr.transcribe(chunk).text
    assert line.text not in asr.transcribe(chunk).text, "each scripted line is heard once"


def test_ac_eval_05_build_local_collaborators(synth_ds, perception_on_path, monkeypatch):
    from perception import fakes

    from nasa_eval.collaborators import GoldDetector, GoldOmni, build_collaborators

    monkeypatch.delenv("DECISION_URL", raising=False)
    c = build_collaborators(synth_ds, "local")
    assert isinstance(c["omni"], GoldOmni) and isinstance(c["detector"], GoldDetector)
    from nasa_eval.collaborators import ThumbnailEmbedder

    assert isinstance(c["embedder"], ThumbnailEmbedder) and isinstance(c["storage"], fakes.MemoryStorage)
    assert isinstance(c["asr"], fakes.FakeASR)
    assert isinstance(c["decision"], fakes.FakeDecision)
    monkeypatch.setenv("DECISION_URL", "http://127.0.0.1:9")
    from perception.decision import DecisionClient

    assert isinstance(build_collaborators(synth_ds, "local")["decision"], DecisionClient)


def test_ac_eval_05_build_cluster_collaborators_from_env(synth_ds, perception_on_path, monkeypatch):
    from perception.asr import AsrClient
    from perception.decision import DecisionClient
    from perception.detector import OwlV2Detector
    from perception.embedder import Dinov2Embedder
    from perception.omni import OmniClient

    from nasa_eval.collaborators import build_collaborators

    for var in ("OMNI_URL", "DECISION_URL", "ASR_URL", "OMNI_REALTIME"):
        monkeypatch.delenv(var, raising=False)
    with pytest.raises(ConfigError, match="OMNI_URL"):
        build_collaborators(synth_ds, "cluster")
    monkeypatch.setenv("OMNI_URL", "http://omni:8000")
    monkeypatch.setenv("DECISION_URL", "http://dgemma:8000")
    monkeypatch.setenv("ASR_URL", "http://asr:8000")
    c = build_collaborators(synth_ds, "cluster")
    assert isinstance(c["omni"], OmniClient) and c["omni"].base_url == "http://omni:8000"
    assert isinstance(c["decision"], DecisionClient) and c["decision"].base_url == "http://dgemma:8000"
    assert isinstance(c["asr"], AsrClient)
    assert isinstance(c["detector"], OwlV2Detector) and isinstance(c["embedder"], Dinov2Embedder)


@pytest.mark.db
@pytest.mark.skipif(importlib.util.find_spec("inventory_api") is None,
                    reason="services/inventory-api not installed in this venv (pip install -e '.[pipeline]')")
def test_ac_eval_05_inventory_api_bound_in_process_to_fresh_db(admin_url, perception_on_path):
    from nasa_eval.collaborators import bind_inventory_api
    from nasa_eval.runner import drop_database, provision_database

    name, url = provision_database(admin_url, DB_PREFIX)
    try:
        api, close = bind_inventory_api(url)
        try:
            assert any(c["catalog_id"] == "drill-pistol" for c in api.get_catalog())
            assert any(loc["location_id"] == "LAB1S2" for loc in api.get_locations())
        finally:
            close()
    finally:
        drop_database(admin_url, name, prefix=DB_PREFIX)


# ---------------------------------------------------------------- Integration phase (found by the real replay)
def _mover(ds):
    return next(g for g in ds.gold if any(e.type == "moved" for e in g.events))


def _downscaled_crop(ds, frames, gold_object, appearance):
    from perception.media import crop, downscale

    return crop(downscale(frames[round(appearance.t * ds.fps)], 448), appearance.bbox)


def test_ac_eval_05_manifest_carries_camera_zones(synth_ds):
    """The synth manifest's camera-configured zone rectangles are loaded (not dropped) for the replay."""
    assert synth_ds.manifest.zones is not None
    assert set(synth_ds.manifest.zones) == {"LAB1S2", "LAB1O3", "LAB1P3"}
    assert all(len(box) == 4 for box in synth_ds.manifest.zones.values())


def test_ac_eval_05_replay_config_applies_manifest_zones(synth_ds, perception_on_path, monkeypatch):
    """Without zones every sighting of the fixed lab camera is module-level (LAB1) and the zone-level
    location_acc gate can never pass; the runner configures perception's camera zones from the manifest."""
    from nasa_eval.collaborators import replay_config

    monkeypatch.delenv("CAMERA_ZONES", raising=False)
    cfg = replay_config(synth_ds)
    cam = synth_ds.manifest.camera
    assert cfg.camera_zones[cam] == {k: tuple(float(v) for v in box) for k, box in synth_ds.manifest.zones.items()}
    # an operator-provided CAMERA_ZONES entry for the same camera wins
    monkeypatch.setenv("CAMERA_ZONES", json.dumps({cam: {"LAB1S2": [0, 0, 1, 1]}}))
    assert replay_config(synth_ds).camera_zones[cam] == {"LAB1S2": (0.0, 0.0, 1.0, 1.0)}


def test_ac_eval_05_local_embedder_reidentifies_synth_crops(synth_ds, perception_on_path):
    """perception's FakeEmbedder keys on the exact dominant colour of painted rectangles; on the synth icons after
    perception's 448-px bilinear downscale a moved object got similarity ~0 and was split into a duplicate. The
    local target's embedder must be appearance-based: 768-d unit vectors, the same object at another position
    >= 0.9, a different object (other class or the unknown) < 0.5."""
    import numpy as np

    from nasa_eval.collaborators import build_collaborators

    emb = build_collaborators(synth_ds, "local")["embedder"]
    frames = _frames(synth_ds)
    mover = _mover(synth_ds)
    first, last = mover.appearances[0], mover.appearances[-1]
    assert first.bbox != last.bbox
    a = np.asarray(emb.embed(_downscaled_crop(synth_ds, frames, mover, first)))
    b = np.asarray(emb.embed(_downscaled_crop(synth_ds, frames, mover, last)))
    assert a.shape == (768,) and abs(float(np.linalg.norm(a)) - 1.0) < 1e-5
    assert float(a @ b) >= 0.9
    assert np.array_equal(a, np.asarray(emb.embed(_downscaled_crop(synth_ds, frames, mover, first))))
    for other in synth_ds.gold:
        if other.catalog_id == mover.catalog_id:
            continue
        o = np.asarray(emb.embed(_downscaled_crop(synth_ds, frames, other, other.appearances[0])))
        assert float(a @ o) < 0.5, other.gold_object_id
