"""Revisit gate (perception AC-PER-32, cluster integration 2026-10-17, docs/QUESTIONS.md #29).

The looped ISS tour created 22 new entities when the camera came back to Columbus about 15 min later. The runner
replays the dataset a second time, shifted by the scene duration + ``REVISIT_GAP_S`` (900 s), after the first pass
has been scored; ``revisit_new_entity_rate`` = entities created during the revisit / gold objects, gated <= 0.05.
In the local target the gold detector misses every other catalog object on the revisit (OWLv2 is intermittent), so
both perception paths (exemplar and class-and-place) are exercised.
"""
from __future__ import annotations

import json
from datetime import timedelta

import pytest
from conftest import DB_PREFIX, EVAL_DIR, TINY_GOLD, DbWriter, write_dataset

from nasa_eval import runner
from nasa_eval.dataset import load_dataset
from nasa_eval.scoring import METRIC_KEYS, score
from nasa_eval.snapshot import RevisitResult, Snapshot, snapshot_from_dict, snapshot_to_dict


def _tiny(tmp_path, frames=12):
    return load_dataset(write_dataset(tmp_path / "datasets", frames=frames))


# ---------------------------------------------------------------- metric
def test_ac_eval_08_metric_key_registered():
    assert "revisit_new_entity_rate" in METRIC_KEYS


def test_ac_eval_08_metric_from_snapshot_revisit(tmp_path):
    data = _tiny(tmp_path)
    snap = Snapshot(revisit=RevisitResult(gap_s=900.0, objects=5, new_entity_ids=["x"]))
    result = score(data, snap)
    assert result["metrics"]["revisit_new_entity_rate"] == pytest.approx(0.2)
    assert result["counts"]["revisit_new_entities"] == 1


def test_ac_eval_08_metric_is_none_without_a_revisit(tmp_path):
    """Not measured -> None, which the gate treats as a failure (never a silent pass)."""
    from nasa_eval.gate import check, load_thresholds

    result = score(_tiny(tmp_path), Snapshot())
    assert result["metrics"]["revisit_new_entity_rate"] is None
    gate = check(result["metrics"], load_thresholds(EVAL_DIR / "thresholds.yaml"))
    assert "revisit_new_entity_rate" in [f.metric for f in gate.failures]


def test_ac_eval_08_threshold_is_gated():
    from nasa_eval.gate import load_thresholds

    th = load_thresholds(EVAL_DIR / "thresholds.yaml")["revisit_new_entity_rate"]
    assert th.max == pytest.approx(0.05) and th.min is None


def test_ac_eval_08_snapshot_json_round_trip():
    snap = Snapshot(revisit=RevisitResult(gap_s=900.0, objects=6, new_entity_ids=["a", "b"]))
    back = snapshot_from_dict(json.loads(json.dumps(snapshot_to_dict(snap))))
    assert back.revisit == snap.revisit
    assert snapshot_from_dict(json.loads(json.dumps(snapshot_to_dict(Snapshot())))).revisit is None


def test_ac_eval_08_reference_and_committed_snapshot_record_a_correct_revisit():
    from nasa_eval.baseline import reference_snapshot
    from nasa_eval.snapshot import load_snapshot_json

    data = load_dataset(EVAL_DIR / "datasets" / "synth-ci")
    ref = reference_snapshot(data)
    assert ref.revisit == RevisitResult(gap_s=runner.REVISIT_GAP_S, objects=len(data.gold), new_entity_ids=[])
    assert load_snapshot_json(EVAL_DIR / "baselines" / "synth-ci.snapshot.json").revisit == ref.revisit


# ---------------------------------------------------------------- runner
def _perfect(dataset, url):
    DbWriter(url).perfect_replay_of(TINY_GOLD)
    return {"windows": 3}


def test_ac_eval_08_run_replays_a_revisit_after_scoring(admin_url, tmp_path):
    ds = write_dataset(tmp_path / "datasets", frames=12)
    calls = []

    def revisit(dataset, url, offset_s):
        calls.append(offset_s)
        DbWriter(url).entity("laptop", status="provisional", last_location="LAB1P3", last_seen_s=offset_s)
        return {"windows": 3}

    res = runner.run(ds, target="local", reports_dir=tmp_path / "r", admin_url=admin_url, db_prefix=DB_PREFIX,
                     replay=_perfect, revisit=revisit)
    assert calls == [pytest.approx(12 + runner.REVISIT_GAP_S)]  # 12 frames at 1 fps + the gap
    assert res.metrics["revisit_new_entity_rate"] == pytest.approx(1 / 5)
    assert res.metrics["pairwise_f1"] == pytest.approx(1.0)  # first-pass metrics are not touched by the revisit
    rep = json.loads(res.json_path.read_text())
    assert rep["revisit_stats"] == {"windows": 3}
    assert rep["counts"]["revisit_new_entities"] == 1


def test_ac_eval_08_run_with_injected_replay_and_no_revisit_reports_none(admin_url, tmp_path):
    ds = write_dataset(tmp_path / "datasets", frames=12)
    res = runner.run(ds, target="local", reports_dir=tmp_path / "r", admin_url=admin_url, db_prefix=DB_PREFIX,
                     replay=_perfect)
    assert res.metrics["revisit_new_entity_rate"] is None


# ---------------------------------------------------------------- local collaborators on the revisit
@pytest.fixture(scope="module")
def synth_ds(tmp_path_factory):
    from nasa_eval.synth import generate_scene, write_scene

    return load_dataset(write_scene(generate_scene(7), tmp_path_factory.mktemp("s"), "synth-7"))


def test_ac_eval_08_revisit_misses_every_other_catalog_object(synth_ds):
    from nasa_eval.collaborators import revisit_misses

    catalog = sorted(g.gold_object_id for g in synth_ds.gold if not g.is_unknown)
    assert revisit_misses(synth_ds) == set(catalog[1::2])


def test_ac_eval_08_gold_detector_skips_missed_objects(synth_ds, perception_on_path):
    import numpy as np
    from PIL import Image

    from nasa_eval.collaborators import GoldDetector, catalog_labels

    labels = catalog_labels()
    frame = np.asarray(Image.open(synth_ds.frame_paths()[-1]).convert("RGB"))
    full = GoldDetector(synth_ds, labels).detect(frame, [])
    t_last = (len(synth_ds.frame_paths()) - 1) / synth_ds.fps
    missed = {next(g.gold_object_id for g in synth_ds.gold
                   if not g.is_unknown and any(abs(a.t - t_last) < 1e-6 for a in g.appearances))}
    partial = GoldDetector(synth_ds, labels, miss=missed).detect(frame, [])
    assert 0 < len(partial) == len(full) - len(missed)


def test_ac_eval_08_gold_omni_maps_shifted_windows_back_to_the_gold(synth_ds, perception_on_path):
    import numpy as np
    from perception.types import Keyframe, Window

    from nasa_eval.collaborators import GoldOmni, catalog_labels

    def window(offset_s):
        start = synth_ds.manifest.start_time + timedelta(seconds=offset_s)
        frames = [Keyframe(timestamp=start + timedelta(seconds=i), image=np.zeros((4, 4, 3), np.uint8), pts=i,
                           index=i) for i in range(30, 36)]
        return Window(t_start=frames[0].timestamp, t_end=frames[-1].timestamp, frames=frames, index=0)

    labels = catalog_labels()
    first = GoldOmni(synth_ds, labels).analyze(window(0)).observation
    again = GoldOmni(synth_ds, labels, time_offset_s=960.0).analyze(window(960.0)).observation
    assert first["objects"] and first["objects"] == again["objects"]
