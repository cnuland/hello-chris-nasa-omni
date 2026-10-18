"""AC-EVAL-5 scenario "omni repeats objects per frame" (perception AC-PER-19, cluster integration 2026-10-17).

On real NASA ISS tour footage, Qwen2.5-Omni-7B listed each object once per frame, every entry with the same coarse
box (services/perception/tests/fixtures/omni_iss_tour_window0.json: 6 laptops + 6 headsets in one window), and
perception created one entity per listing. The old GoldOmni reported each gold object once per window, so the eval
gate could not see this. GoldOmni's ``repeat_per_frame`` profile (set by EVAL_OMNI_PROFILE; the default ``gemma_people`` adds people[] on
top of it, perception AC-PER-38) answers the way the real model did; ``once`` keeps the old behaviour.
"""
from __future__ import annotations

import importlib.util
import json
from collections import Counter
from datetime import timedelta

import pytest
from conftest import EVAL_DIR

from nasa_eval import runner
from nasa_eval.dataset import load_dataset
from nasa_eval.runner import ConfigError

PIPELINE_AVAILABLE = (
    importlib.util.find_spec("perception") is not None and importlib.util.find_spec("inventory_api") is not None
)


@pytest.fixture(scope="module")
def synth_ds(tmp_path_factory):
    from nasa_eval.synth import generate_scene, write_scene

    return load_dataset(write_scene(generate_scene(7), tmp_path_factory.mktemp("s"), "synth-7"))


def _window(ds, first, n=6):
    import numpy as np
    from perception.types import Keyframe, Window
    from PIL import Image

    paths = ds.frame_paths()
    kfs = [Keyframe(ds.manifest.start_time + timedelta(seconds=k / ds.fps),
                    np.asarray(Image.open(paths[k]).convert("RGB")), pts=k / ds.fps, index=k)
           for k in range(first, min(first + n, len(paths)))]
    return Window(kfs[0].timestamp, kfs[-1].timestamp, kfs, None, 0)


def _present(ds, window):
    """gold_object_id -> window frame indexes where the object appears."""
    out: dict[str, list[int]] = {}
    for k, kf in enumerate(window.frames):
        t = (kf.timestamp - ds.manifest.start_time).total_seconds()
        for g in ds.gold:
            if any(abs(a.t - t) < 1e-6 for a in g.appearances):
                out.setdefault(g.gold_object_id, []).append(k)
    return out


def test_ac_eval_05_gold_omni_default_profile_repeats_per_frame(synth_ds, perception_on_path):
    from nasa_eval.collaborators import DEFAULT_OMNI_PROFILE, GoldOmni, catalog_labels

    # AC-PER-38: the default is now gemma_people, which keeps the per-frame object repeats and adds people[]
    assert DEFAULT_OMNI_PROFILE == "gemma_people"
    assert GoldOmni(synth_ds, catalog_labels()).profile == "gemma_people"
    window = _window(synth_ds, 0)
    repeat = GoldOmni(synth_ds, catalog_labels(), profile="repeat_per_frame").analyze(window).observation
    gemma = GoldOmni(synth_ds, catalog_labels()).analyze(window).observation
    assert gemma["objects"] == repeat["objects"]


def test_ac_eval_05_gold_omni_repeat_profile_lists_objects_once_per_frame_with_one_box(synth_ds, perception_on_path):
    from perception.schema import validate_observation

    from nasa_eval.collaborators import GoldOmni, catalog_labels

    window = _window(synth_ds, 0)
    obs = GoldOmni(synth_ds, catalog_labels(), profile="repeat_per_frame").analyze(window).observation
    assert validate_observation(obs) == []
    present = _present(synth_ds, window)
    assert len(obs["objects"]) == sum(len(v) for v in present.values()) > len(present)
    by_label = Counter(o["label"] for o in obs["objects"])
    assert max(by_label.values()) >= len(window.frames)  # e.g. 6 x "laptop", like the real reply
    # omni does not localise per frame: all repeats of one object share one box (one distinct box per object)
    assert len({tuple(o["bbox"]) for o in obs["objects"]}) == len(present)
    assert all(0 <= o["frame_index"] < len(window.frames) for o in obs["objects"])


def test_ac_eval_05_gold_omni_once_profile_keeps_one_entry_per_object(synth_ds, perception_on_path):
    from nasa_eval.collaborators import GoldOmni, catalog_labels

    window = _window(synth_ds, 0)
    obs = GoldOmni(synth_ds, catalog_labels(), profile="once").analyze(window).observation
    assert len(obs["objects"]) == len(_present(synth_ds, window))


def test_ac_eval_05_gold_omni_rejects_unknown_profile(synth_ds, perception_on_path):
    from nasa_eval.collaborators import GoldOmni, catalog_labels

    with pytest.raises(ValueError, match="profile"):
        GoldOmni(synth_ds, catalog_labels(), profile="sometimes")


def test_ac_eval_05_omni_profile_from_environment(synth_ds, perception_on_path, monkeypatch):
    from nasa_eval.collaborators import build_collaborators

    monkeypatch.delenv("DECISION_URL", raising=False)
    monkeypatch.delenv("EVAL_OMNI_PROFILE", raising=False)
    assert build_collaborators(synth_ds, "local")["omni"].profile == "gemma_people"
    monkeypatch.setenv("EVAL_OMNI_PROFILE", "once")
    assert build_collaborators(synth_ds, "local")["omni"].profile == "once"
    monkeypatch.setenv("EVAL_OMNI_PROFILE", "bogus")
    with pytest.raises(ConfigError, match="EVAL_OMNI_PROFILE"):
        build_collaborators(synth_ds, "local")


@pytest.mark.integration
@pytest.mark.db
@pytest.mark.skipif(not PIPELINE_AVAILABLE, reason="services/perception and services/inventory-api must be importable")
@pytest.mark.parametrize("profile", ["repeat_per_frame", "once"])
def test_ac_eval_05_integration_omni_repeating_objects_per_frame_passes_the_gate(admin_url, tmp_path, monkeypatch,
                                                                                 profile):
    """The scenario that would have caught the cluster failure: a model that repeats each object in every frame
    must not create duplicates (duplicate_rate gate) and both profiles must pass the strict gate."""
    from nasa_eval.gate import check, load_thresholds
    from nasa_eval.synth import generate_scene, write_scene

    for var in ("EVAL_INVENTORY_API_URL", "CAMERA_ZONES", "DECISION_URL"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("EVAL_OMNI_PROFILE", profile)
    d = write_scene(generate_scene(7), tmp_path / "datasets", "synth-7")
    res = runner.run(d, target="local", reports_dir=tmp_path / "reports", admin_url=admin_url,
                     db_prefix="test_int_")
    rep = json.loads(res.json_path.read_text())
    assert rep["replay_stats"]["posted"] == rep["replay_stats"]["windows"] > 0
    assert res.metrics["duplicate_rate"] <= 0.10, res.metrics["duplicate_rate"]
    gate = check(res.metrics, load_thresholds(EVAL_DIR / "thresholds.yaml"))
    assert gate.passed, gate.failures
