"""Person tracks and sleep-cue privacy gates (perception AC-PER-35..38, Gemma 4 run 2026-10-18).

Gemma 4 reports people only in the observation's ``people[]`` (never as "person" objects), and perception silently
created 0 person tracks. GoldOmni's ``gemma_people`` profile (the default) answers like that, with every gold actor in
every window; every profile lists a sleeping crew member (the live Gemma text) in one quiet window of the scene.
Metrics: ``person_track_recall`` (>= 0.99), ``person_tracks_per_person`` (<= 1.3 since perception AC-PER-48), ``sleep_window_media`` (<= 0)."""
from __future__ import annotations

import importlib.util
import json
from datetime import timedelta

import pytest
from conftest import EVAL_DIR

from nasa_eval import runner
from nasa_eval.dataset import load_dataset
from nasa_eval.snapshot import PredEntity, PredSighting, PredWindow, Snapshot, snapshot_from_dict, snapshot_to_dict

PIPELINE_AVAILABLE = (
    importlib.util.find_spec("perception") is not None and importlib.util.find_spec("inventory_api") is not None
)
SLEEPING = "dark hair, sleeping in a blue sleeping bag"


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


def _actors(ds) -> list[str]:
    return sorted({e.actor for g in ds.gold for e in g.events if e.actor})


# ------------------------------------------------------------------------------------------------ GoldOmni
def test_ac_eval_09_default_profile_is_gemma_people(synth_ds, perception_on_path, monkeypatch):
    from nasa_eval.collaborators import (
        DEFAULT_OMNI_PROFILE,
        OMNI_PROFILES,
        GoldOmni,
        build_collaborators,
        catalog_labels,
    )

    monkeypatch.delenv("EVAL_OMNI_PROFILE", raising=False)
    monkeypatch.delenv("DECISION_URL", raising=False)
    assert "gemma_people" in OMNI_PROFILES and DEFAULT_OMNI_PROFILE == "gemma_people"
    assert GoldOmni(synth_ds, catalog_labels()).profile == "gemma_people"
    assert build_collaborators(synth_ds, "local")["omni"].profile == "gemma_people"


def test_ac_eval_09_gemma_people_lists_every_actor_in_every_window_only_in_people(synth_ds, perception_on_path):
    from perception.schema import validate_observation

    from nasa_eval.collaborators import GoldOmni, catalog_labels

    omni = GoldOmni(synth_ds, catalog_labels(), profile="gemma_people")
    actors = _actors(synth_ds)
    assert len(actors) >= 2
    texts: set[str] = set()
    for first in (8, 20, 40):
        obs = omni.analyze(_window(synth_ds, first)).observation
        assert validate_observation(obs) == []
        assert not [o for o in obs["objects"] if o["label"].lower() in ("person", "crew member", "astronaut")]
        assert len(obs["people"]) == len(actors)
        refs = [p["ref"] for p in obs["people"]]
        assert len(set(refs)) == len(refs)
        for p, actor in zip(obs["people"], actors, strict=True):
            colour_garment = actor.split(", ", 1)[1]  # "blue shirt"
            assert colour_garment in p["appearance"]
            texts.add(p["appearance"])
    assert len(texts) > len(actors)  # Gemma-like variation between windows


def test_ac_eval_09_event_actor_keeps_the_exact_gold_text(synth_ds, perception_on_path):
    from nasa_eval.collaborators import GoldOmni, catalog_labels

    omni = GoldOmni(synth_ds, catalog_labels(), profile="gemma_people")
    paths = synth_ds.frame_paths()
    found = 0
    for first in range(0, len(paths) - 5, 4):
        obs = omni.analyze(_window(synth_ds, first)).observation
        people = {p["ref"]: p["appearance"] for p in obs["people"]}
        for ev in obs["events"]:
            if ev.get("actor_ref"):
                assert people[ev["actor_ref"]] in _actors(synth_ds)
                found += 1
    assert found >= 1


@pytest.mark.parametrize("profile", ["gemma_people", "repeat_per_frame", "once", "truncated"])
def test_ac_eval_09_one_quiet_window_has_a_sleeping_crew_member(synth_ds, perception_on_path, profile):
    """Exactly one window of a pass (the first quiet one: no event, unknown or transcript line near it) lists the
    sleeping crew member, with a neutral, cue-free summary that the scorer uses to find it."""
    from perception.people import sleep_cue

    from nasa_eval.collaborators import GoldOmni, catalog_labels
    from nasa_eval.scoring import SLEEP_SUMMARY

    omni = GoldOmni(synth_ds, catalog_labels(), profile=profile)
    paths = synth_ds.frame_paths()
    sleepy = []
    for first in range(0, len(paths) - 5, 4):
        obs = omni.analyze(_window(synth_ds, first)).observation
        if SLEEPING in [p["appearance"] for p in obs["people"]]:
            sleepy.append(first)
            assert obs["scene_summary"] == SLEEP_SUMMARY and sleep_cue(SLEEP_SUMMARY) is None
            lo, hi = first / synth_ds.fps, (first + 5) / synth_ds.fps
            assert not [e for g in synth_ds.gold for e in g.events if lo - 6 <= e.t <= hi + 6]
            assert not [ln for ln in synth_ds.transcript_lines() if lo - 6 <= ln.t <= hi + 6]
            assert not [a for g in synth_ds.gold if g.is_unknown for a in g.appearances if lo - 1 <= a.t <= hi + 1]
        else:
            assert obs["scene_summary"] != SLEEP_SUMMARY
    assert len(sleepy) == 1, sleepy


# ------------------------------------------------------------------------------------------------ scoring
def _snap(ds, *, persons=(), windows=(), sightings=()) -> Snapshot:
    snap = Snapshot()
    for i, desc in enumerate(persons):
        snap.entities[f"p{i}"] = PredEntity(f"p{i}", "crew-member", "provisional", "person", "LAB1",
                                            ds.manifest.start_time, description=desc)
    snap.windows = list(windows)
    snap.sightings = list(sightings)
    return snap


def test_ac_eval_09_person_track_recall_and_split(synth_ds):
    from nasa_eval.scoring import person_track_recall, person_tracks_per_person

    actors = _actors(synth_ds)
    assert person_track_recall(synth_ds, _snap(synth_ds)) == 0.0  # the Gemma regression: no person tracks at all
    assert person_tracks_per_person(synth_ds, _snap(synth_ds)) == 0.0
    tracks = [f"astronaut wearing a {a.split(', ', 1)[1]}" for a in actors]
    assert person_track_recall(synth_ds, _snap(synth_ds, persons=tracks)) == 1.0
    assert person_track_recall(synth_ds, _snap(synth_ds, persons=tracks[:1])) == pytest.approx(1 / len(actors))
    split = _snap(synth_ds, persons=tracks * 2)
    assert person_tracks_per_person(synth_ds, split) == 2.0
    # an object entity is never a person track
    snap = _snap(synth_ds)
    snap.entities["o"] = PredEntity("o", "laptop", "provisional", "object", description=actors[0])
    assert person_track_recall(synth_ds, snap) == 0.0


def test_ac_eval_09_sleep_window_media(synth_ds):
    from nasa_eval.scoring import SLEEP_SUMMARY, sleep_window_media

    t0 = synth_ds.manifest.start_time
    sleep = PredWindow("w0", t0, t0 + timedelta(seconds=5), None, SLEEP_SUMMARY)
    later = PredWindow("w1", t0 + timedelta(seconds=4), t0 + timedelta(seconds=9), "s3://b/raw/1.json", "objects")
    clean = [PredSighting("s1", "e", t0, "LAB1", None, window_id="w0", frame_uri=None),
             PredSighting("s2", "e", t0 + timedelta(seconds=5), "LAB1", None, window_id="w1", frame_uri="s3://f")]
    assert sleep_window_media(synth_ds, _snap(synth_ds, windows=[sleep, later], sightings=clean)) == 0
    leaky_window = PredWindow("w0", t0, t0 + timedelta(seconds=5), "s3://b/raw/0.json", SLEEP_SUMMARY)
    leaky = clean + [PredSighting("s3", "e", t0, "LAB1", None, window_id="w0", frame_uri="s3://b/frames/0.jpg")]
    assert sleep_window_media(synth_ds, _snap(synth_ds, windows=[leaky_window, later], sightings=leaky)) == 2
    assert sleep_window_media(synth_ds, _snap(synth_ds, windows=[later])) is None  # not measured -> gate fails


def test_ac_eval_09_metrics_are_reported_and_gated(synth_ds):
    from nasa_eval.gate import check, load_thresholds
    from nasa_eval.scoring import METRIC_KEYS, score

    for key in ("person_track_recall", "person_tracks_per_person", "sleep_window_media"):
        assert key in METRIC_KEYS
    th = load_thresholds(EVAL_DIR / "thresholds.yaml")
    assert th["person_track_recall"].min == pytest.approx(0.99)
    assert th["person_tracks_per_person"].max == pytest.approx(1.3)  # AC-PER-48 (was 1.5)
    assert th["sleep_window_media"].max == 0
    failed = {f.metric for f in check(score(synth_ds, _snap(synth_ds))["metrics"], th).failures}
    assert {"person_track_recall", "sleep_window_media"} <= failed


def test_ac_eval_09_snapshot_json_round_trip_keeps_people_and_windows(synth_ds):
    t0 = synth_ds.manifest.start_time
    snap = _snap(synth_ds, persons=["crew member, blue shirt"],
                 windows=[PredWindow("w0", t0, t0 + timedelta(seconds=5), None, "resting")],
                 sightings=[PredSighting("s1", "p0", t0, "LAB1", None, window_id="w0", frame_uri="s3://f")])
    back = snapshot_from_dict(json.loads(json.dumps(snapshot_to_dict(snap))))
    assert back.entities["p0"].description == "crew member, blue shirt" and back.entities["p0"].kind == "person"
    assert back.windows == snap.windows
    assert back.sightings[0].window_id == "w0" and back.sightings[0].frame_uri == "s3://f"


def test_ac_eval_09_reference_snapshot_passes_the_new_gates():
    from nasa_eval.baseline import reference_snapshot
    from nasa_eval.scoring import score

    ds = load_dataset(EVAL_DIR / "datasets" / "synth-ci")
    m = score(ds, reference_snapshot(ds))["metrics"]
    assert m["person_track_recall"] == 1.0 and m["person_tracks_per_person"] == 1.0
    assert m["sleep_window_media"] == 0


# ------------------------------------------------------------------------------------------------ integration
@pytest.mark.integration
@pytest.mark.db
@pytest.mark.skipif(not PIPELINE_AVAILABLE, reason="services/perception and services/inventory-api must be importable")
def test_ac_eval_09_integration_gemma_people_passes_the_gate(admin_url, tmp_path, monkeypatch):
    from nasa_eval.gate import check, load_thresholds
    from nasa_eval.synth import generate_scene, write_scene

    for var in ("EVAL_INVENTORY_API_URL", "CAMERA_ZONES", "DECISION_URL"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("EVAL_OMNI_PROFILE", "gemma_people")
    d = write_scene(generate_scene(7), tmp_path / "datasets", "synth-7")
    res = runner.run(d, target="local", reports_dir=tmp_path / "reports", admin_url=admin_url, db_prefix="test_int_")
    assert res.metrics["person_track_recall"] == 1.0, res.metrics
    assert res.metrics["sleep_window_media"] == 0, res.metrics
    gate = check(res.metrics, load_thresholds(EVAL_DIR / "thresholds.yaml"))
    assert gate.passed, gate.failures


def test_review_sleep_window_person_sightings_are_gated(synth_ds):
    """Review round: a person track of the sleeper is a leak even when no image was stored."""
    from nasa_eval.gate import load_thresholds
    from nasa_eval.scoring import METRIC_KEYS, SLEEP_SUMMARY, sleep_window_person_sightings

    t0 = synth_ds.manifest.start_time
    sleep = PredWindow("w0", t0, t0 + timedelta(seconds=5), None, SLEEP_SUMMARY)
    snap = _snap(synth_ds, persons=[SLEEPING], windows=[sleep],
                 sightings=[PredSighting("s1", "p0", t0, "LAB1", None, window_id="w0")])
    assert sleep_window_person_sightings(synth_ds, snap) == 1
    snap.entities["p0"] = PredEntity("p0", "laptop", "provisional", "object")
    assert sleep_window_person_sightings(synth_ds, snap) == 0  # objects are still sighted in private windows
    assert sleep_window_person_sightings(synth_ds, _snap(synth_ds)) is None
    assert "sleep_window_person_sightings" in METRIC_KEYS
    assert load_thresholds(EVAL_DIR / "thresholds.yaml")["sleep_window_person_sightings"].max == 0
