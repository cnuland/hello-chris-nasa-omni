"""AC-EVAL-5 scenario "omni reply cut off at max_tokens" (perception AC-PER-26/27, cluster integration 2026-10-17).

On the cluster 18 of 53 Qwen2.5-Omni-7B replies (34 %) hit max_tokens and were cut off mid-JSON; perception retried
and then dropped those windows. GoldOmni's ``truncated`` profile answers like that verbose model: objects repeated
per frame, a long transcript echo, and the reply cut off inside the transcript with finish_reason "length". The
reply goes through perception's own ``interpret_reply`` (salvage + object cap), so the strict gate covers salvage.
"""
from __future__ import annotations

import importlib.util
import json

import pytest
from conftest import EVAL_DIR
from test_ac_eval_05_omni_repeats import _present, _window

from nasa_eval import runner

PIPELINE_AVAILABLE = (
    importlib.util.find_spec("perception") is not None and importlib.util.find_spec("inventory_api") is not None
)


@pytest.fixture(scope="module")
def synth_ds(tmp_path_factory):
    from nasa_eval.dataset import load_dataset
    from nasa_eval.synth import generate_scene, write_scene

    return load_dataset(write_scene(generate_scene(7), tmp_path_factory.mktemp("s"), "synth-7"))


def test_ac_eval_05_truncated_profile_is_known():
    from nasa_eval.collaborators import OMNI_PROFILES

    assert "truncated" in OMNI_PROFILES


def test_ac_eval_05_truncated_profile_reply_is_cut_off_and_salvaged(synth_ds, perception_on_path):
    from perception.schema import extract_first_json_object, validate_observation

    from nasa_eval.collaborators import GoldOmni, catalog_labels

    window = _window(synth_ds, 0)
    res = GoldOmni(synth_ds, catalog_labels(), profile="truncated").analyze(window)
    assert res.ok and res.truncated is True and res.finish_reason == "length"
    assert extract_first_json_object(res.raw) is None or "scene_summary" not in extract_first_json_object(res.raw)
    assert not res.raw.rstrip().endswith("}")
    obs = res.observation
    assert validate_observation(obs) == []
    assert obs["transcript"] == ""  # cut off inside the transcript echo; ASR is the transcript of record
    # every gold object of the window survives the cut and the cap (label-diverse: one listing per object at least)
    labels = {o["label"] for o in obs["objects"]}
    full = GoldOmni(synth_ds, catalog_labels(), profile="repeat_per_frame").analyze(window).observation
    assert labels == {o["label"] for o in full["objects"]}
    assert len(_present(synth_ds, window)) <= len(obs["objects"]) <= 12


def test_ac_eval_05_truncated_profile_from_environment(synth_ds, perception_on_path, monkeypatch):
    from nasa_eval.collaborators import build_collaborators

    monkeypatch.delenv("DECISION_URL", raising=False)
    monkeypatch.setenv("EVAL_OMNI_PROFILE", "truncated")
    assert build_collaborators(synth_ds, "local")["omni"].profile == "truncated"


@pytest.mark.integration
@pytest.mark.db
@pytest.mark.skipif(not PIPELINE_AVAILABLE, reason="services/perception and services/inventory-api must be importable")
def test_ac_eval_05_integration_truncated_replies_are_salvaged_and_pass_the_gate(admin_url, tmp_path, monkeypatch):
    """Every window's reply is cut off: perception must salvage (not drop) them and still pass the strict gate."""
    from nasa_eval.gate import check, load_thresholds
    from nasa_eval.synth import generate_scene, write_scene

    for var in ("EVAL_INVENTORY_API_URL", "CAMERA_ZONES", "DECISION_URL"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("EVAL_OMNI_PROFILE", "truncated")
    d = write_scene(generate_scene(7), tmp_path / "datasets", "synth-7")
    res = runner.run(d, target="local", reports_dir=tmp_path / "reports", admin_url=admin_url,
                     db_prefix="test_int_")
    rep = json.loads(res.json_path.read_text())
    assert rep["replay_stats"]["invalid"] == 0
    assert rep["replay_stats"]["posted"] == rep["replay_stats"]["windows"] > 0
    gate = check(res.metrics, load_thresholds(EVAL_DIR / "thresholds.yaml"))
    assert gate.passed, gate.failures
