"""Crew transit gate (perception AC-PER-45..48, live Gemma 4 run 2026-10-18: 25 person tracks for 7 to 11 crew).

On the ISS the crew float between modules and the handheld camera follows them; perception matched person sightings
only within one module, so every module started a new track. After the revisit pass the local runner replays a
*crew transit* pass: the scene's frames through the roaming camera ``cam-handheld``, GoldOmni profile
``crew_transit`` (no objects / events / transcript, every gold actor in ``people[]``, a location hint naming Unity,
Columbus, then Kibo by thirds of the scene). ``person_tracks_per_person`` is measured on the final database (all
passes) and gated <= 1.3."""
from __future__ import annotations

import importlib.util
import json
from datetime import timedelta

import pytest
from conftest import DB_PREFIX, EVAL_DIR, DbWriter, write_dataset

from nasa_eval import runner
from nasa_eval.dataset import load_dataset

PIPELINE_AVAILABLE = (
    importlib.util.find_spec("perception") is not None and importlib.util.find_spec("inventory_api") is not None
)


@pytest.fixture(scope="module")
def synth_ds(tmp_path_factory):
    from nasa_eval.synth import generate_scene, write_scene

    return load_dataset(write_scene(generate_scene(7), tmp_path_factory.mktemp("s"), "synth-7"))


def _window(ds, first, n=6, offset_s=0.0):
    import numpy as np
    from perception.types import Keyframe, Window
    from PIL import Image

    paths = ds.frame_paths()
    t0 = ds.manifest.start_time + timedelta(seconds=offset_s)
    kfs = [Keyframe(t0 + timedelta(seconds=k / ds.fps), np.asarray(Image.open(paths[k]).convert("RGB")),
                    pts=k / ds.fps, index=k)
           for k in range(first, min(first + n, len(paths)))]
    return Window(kfs[0].timestamp, kfs[-1].timestamp, kfs, None, 0)


def _actors(ds) -> list[str]:
    return sorted({e.actor for g in ds.gold for e in g.events if e.actor})


# ------------------------------------------------------------------------------------------------ GoldOmni
def test_crew_transit_profile_lists_only_people_with_a_module_hint(synth_ds, perception_on_path):
    from nasa_eval.collaborators import TRANSIT_MODULE_HINTS, TRANSIT_PROFILE, GoldOmni, catalog_labels

    assert TRANSIT_MODULE_HINTS == ("Unity", "Columbus", "Kibo")  # NOD1, COL1, JPM1 (perception AC-PER-39 aliases)
    offset = 1200.0
    omni = GoldOmni(synth_ds, catalog_labels(), profile=TRANSIT_PROFILE, time_offset_s=offset)
    n = len(synth_ds.frame_paths())
    hints = []
    for first in range(0, n - 5, 6):
        obs = omni.analyze(_window(synth_ds, first, offset_s=offset)).observation
        assert obs["objects"] == [] and obs["events"] == [] and obs["transcript"] == ""
        assert len(obs["people"]) == len(_actors(synth_ds))
        assert all("sleeping" not in p["appearance"] for p in obs["people"])
        hints.append(obs["location_hint"])
    assert sorted(set(hints), key=hints.index) == list(TRANSIT_MODULE_HINTS)  # each module in turn, in order


def test_crew_transit_profile_is_not_a_main_pass_profile():
    from nasa_eval.collaborators import OMNI_PROFILES, TRANSIT_PROFILE

    assert TRANSIT_PROFILE not in OMNI_PROFILES


# ------------------------------------------------------------------------------------------------ gate
def test_person_tracks_per_person_gate_is_1_3():
    from nasa_eval.gate import load_thresholds

    assert load_thresholds(EVAL_DIR / "thresholds.yaml")["person_tracks_per_person"].max == pytest.approx(1.3)


# ------------------------------------------------------------------------------------------------ runner
def _perfect(dataset, url):
    from conftest import TINY_GOLD

    DbWriter(url).perfect_replay_of(TINY_GOLD)
    return {"windows": 3}


@pytest.mark.db
def test_runner_replays_the_transit_last_and_rescores_person_tracks(admin_url, tmp_path):
    ds = write_dataset(tmp_path / "datasets", frames=12)
    calls = []

    def revisit(dataset, url, offset_s):
        calls.append(("revisit", offset_s))
        return {}

    def transit(dataset, url, offset_s):
        calls.append(("transit", offset_s))
        DbWriter(url).entity("crew-member", kind="person", last_location="NOD1", last_seen_s=offset_s)
        return {"windows": 2}

    res = runner.run(ds, target="local", reports_dir=tmp_path / "r", admin_url=admin_url, db_prefix=DB_PREFIX,
                     replay=_perfect, revisit=revisit, transit=transit)
    assert [c[0] for c in calls] == ["revisit", "transit"]
    assert calls[1][1] == pytest.approx(2 * 12 + runner.REVISIT_GAP_S + runner.TRANSIT_GAP_S)
    rep = json.loads(res.json_path.read_text())
    assert rep["transit_stats"] == {"windows": 2}
    assert rep["counts"]["transit_new_person_tracks"] == 1
    assert res.metrics["person_tracks_per_person"] is not None


@pytest.mark.db
def test_runner_with_injected_replay_runs_no_transit(admin_url, tmp_path, monkeypatch):
    ds = write_dataset(tmp_path / "datasets", frames=12)
    monkeypatch.setattr(runner, "default_transit", lambda *a, **k: pytest.fail("transit must not run"))
    res = runner.run(ds, target="local", reports_dir=tmp_path / "r", admin_url=admin_url, db_prefix=DB_PREFIX,
                     replay=_perfect)
    assert "transit_stats" not in json.loads(res.json_path.read_text())


@pytest.mark.db
def test_review_runner_remeasures_person_track_recall_after_the_transit(admin_url, tmp_path, monkeypatch):
    """A transit that merges two actors into one track must show up: the gate on tracks per person is a max only."""
    ds = write_dataset(tmp_path / "datasets", frames=12)
    monkeypatch.setattr(runner, "person_track_recall", lambda data, snap: 0.5)
    res = runner.run(ds, target="local", reports_dir=tmp_path / "r", admin_url=admin_url, db_prefix=DB_PREFIX,
                     replay=_perfect, revisit=lambda d, u, o: {}, transit=lambda d, u, o: {})
    assert res.metrics["person_track_recall"] == 0.5


# ------------------------------------------------------------------------------------------------ integration
@pytest.mark.integration
@pytest.mark.db
@pytest.mark.skipif(not PIPELINE_AVAILABLE, reason="services/perception and services/inventory-api must be importable")
def test_integration_crew_crossing_modules_keeps_one_track_each(admin_url, tmp_path, monkeypatch):
    from nasa_eval.synth import generate_scene, write_scene

    for var in ("EVAL_INVENTORY_API_URL", "CAMERA_ZONES", "DECISION_URL", "PERCEPTION_PERSON_CROSS_MODULE_S"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("EVAL_OMNI_PROFILE", "gemma_people")
    d = write_scene(generate_scene(7), tmp_path / "datasets", "synth-7")
    res = runner.run(d, target="local", reports_dir=tmp_path / "reports", admin_url=admin_url, db_prefix="test_int_")
    rep = json.loads(res.json_path.read_text())
    assert rep["transit_stats"]["windows"] > 0
    assert res.metrics["person_tracks_per_person"] <= 1.3, (res.metrics, rep["counts"])
    assert res.metrics["person_track_recall"] == 1.0
