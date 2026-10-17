"""AC-EVAL-3 (applied): scoring a pipeline result (Snapshot of the inventory DB) against gold.

Covers sighting -> gold matching, the flat metrics dict used by reports/gates, and reading a Snapshot from a real
(test_eval_*) Postgres database built from contracts/db/*.sql.
"""
from __future__ import annotations

import math

import pytest
from conftest import EVAL_DIR, SEED_LOCATIONS, TINY_GOLD, app, at, gold_obj, write_dataset

from nasa_eval.dataset import load_dataset
from nasa_eval.gate import check, load_thresholds
from nasa_eval.scoring import (
    DEFAULT_MATCH_TOLERANCE_S,
    METRIC_KEYS,
    appearance_key,
    gold_locations_at,
    match_sightings,
    score,
)
from nasa_eval.snapshot import PredEntity, PredEvent, PredReview, PredSighting, Snapshot, read_snapshot


@pytest.fixture
def data(tmp_path):
    return load_dataset(write_dataset(tmp_path, frames=12))


def build_snapshot(groups: dict[str, list[tuple[str, int]]], *, catalog: dict[str, str | None],
                   last_location: dict[str, str] | None = None, events=(), reviews=()) -> Snapshot:
    """groups: entity_id -> [(gold_object_id, appearance_index)] ; sightings copied from TINY_GOLD appearances."""
    gold = {g["gold_object_id"]: g for g in TINY_GOLD}
    snap = Snapshot(locations=dict(SEED_LOCATIONS))
    n = 0
    for eid, refs in groups.items():
        last_t, last_loc = -1.0, None
        for gid, idx in refs:
            a = gold[gid]["appearances"][idx]
            n += 1
            snap.sightings.append(PredSighting(f"s{n}", eid, at(a["t"]), a["location"], tuple(a["bbox"])))
            if a["t"] >= last_t:
                last_t, last_loc = a["t"], a["location"]
        loc = (last_location or {}).get(eid, last_loc)
        snap.entities[eid] = PredEntity(eid, catalog[eid], "unknown" if catalog[eid] is None else "provisional",
                                        last_location=loc, last_seen_at=at(last_t))
    snap.events.extend(events)
    snap.reviews.extend(reviews)
    return snap


def perfect_snapshot() -> Snapshot:
    groups, catalog, events, reviews = {}, {}, [], []
    for g in TINY_GOLD:
        eid = "E-" + g["gold_object_id"]
        groups[eid] = [(g["gold_object_id"], i) for i in range(len(g["appearances"]))]
        catalog[eid] = g["catalog_id"]
        events += [PredEvent(e["type"], eid, at(e["t"]), e.get("actor"), e.get("to_location")) for e in g["events"]]
        if g["is_unknown"]:
            reviews.append(PredReview("unknown_object", eid))
    return build_snapshot(groups, catalog=catalog, events=events, reviews=reviews)


# ---------------------------------------------------------------- helpers
def test_ac_eval_03_gold_locations_at_audit_time(data):
    # review round: events count too. laptop-1 is 'removed' at t=6, so from then on it is out of scope (not expected
    # in its last zone any more); before that it is still in LAB1P3.
    assert gold_locations_at(data.gold, 10) == {"drill-1": "LAB1P3", "wrench-a": "LAB1S2", "wrench-b": "LAB1O3",
                                               "unk-1": "LAB1O3"}
    assert gold_locations_at(data.gold, 5)["drill-1"] == "LAB1S2"
    assert gold_locations_at(data.gold, 5)["laptop-1"] == "LAB1P3"
    assert gold_locations_at(data.gold, 0.5) == {"drill-1": "LAB1S2", "laptop-1": "LAB1P3"}


def test_ac_eval_03_gold_location_follows_moved_event_without_appearance(tmp_path):
    gold = [gold_obj("drill-1", "drill-pistol", [app(0, "LAB1S2", [0.1, 0.1, 0.2, 0.2])],
                     [{"type": "moved", "t": 5.0, "actor": "crew", "to_location": "NOD2D4"}])]
    d = load_dataset(write_dataset(tmp_path, gold=gold))
    assert gold_locations_at(d.gold, 4) == {"drill-1": "LAB1S2"}
    assert gold_locations_at(d.gold, 6) == {"drill-1": "NOD2D4"}


def test_ac_eval_03_match_sightings_by_time_and_iou(data):
    sightings = [
        PredSighting("p1", "E1", at(0), "LAB1S2", (0.11, 0.10, 0.21, 0.20)),   # drill@0
        PredSighting("p2", "E2", at(2), "LAB1O3", (0.30, 0.50, 0.40, 0.60)),   # twin b, not a
        PredSighting("p3", "E3", at(0), "LAB1S2", (0.95, 0.95, 0.99, 0.99)),   # nothing there -> unmatched
        PredSighting("p4", "E4", at(5.3), "LAB1O3", None),                      # no bbox: time only -> unk@0
        PredSighting("p5", "E1", at(0), "LAB1S2", (0.10, 0.10, 0.20, 0.20)),   # drill@0 already taken -> unmatched
    ]
    got = match_sightings(sightings, data)
    assert got["p1"] == appearance_key("drill-1", 0)
    assert got["p2"] == appearance_key("wrench-b", 0)
    assert got["p4"] == appearance_key("unk-1", 0)
    assert "p3" not in got and "p5" not in got


def test_ac_eval_03_appearance_key_format():
    assert appearance_key("drill-1", 2) == "drill-1@2"


# ---------------------------------------------------------------- score()
def test_ac_eval_03_score_perfect_run(data):
    result = score(data, perfect_snapshot())
    mt = result["metrics"]
    assert set(METRIC_KEYS) <= set(mt)
    for k in ("detection_macro_f1", "pairwise_precision", "pairwise_recall", "pairwise_f1", "bcubed_precision",
              "bcubed_recall", "bcubed_f1", "location_acc", "location_module_acc", "event_precision",
              "event_recall", "event_f1", "actor_accuracy", "unknown_recall"):
        assert mt[k] == pytest.approx(1.0), k
    assert mt["duplicate_rate"] == 0.0 and mt["false_merge_rate"] == 0.0
    assert result["entity_to_gold"] == {"E-" + g["gold_object_id"]: g["gold_object_id"] for g in TINY_GOLD}
    assert set(result["per_class"]) >= {"drill-pistol", "wrench", "laptop", "unknown"}
    assert result["counts"]["gold_objects"] == 5


def test_ac_eval_03_score_split_duplicate(data):
    """drill split in two entities: duplicate_rate = (6 - 5) / 5; location uses the most recently seen entity."""
    groups = {"D1": [("drill-1", 0), ("drill-1", 1)], "D2": [("drill-1", 2)], "WA": [("wrench-a", 0), ("wrench-a", 1)],
              "WB": [("wrench-b", 0), ("wrench-b", 1)], "U": [("unk-1", 0)], "L": [("laptop-1", 0)]}
    catalog = {"D1": "drill-pistol", "D2": "drill-pistol", "WA": "wrench", "WB": "wrench", "U": None, "L": "laptop"}
    mt = score(data, build_snapshot(groups, catalog=catalog, reviews=[PredReview("unknown_object", "U")]))["metrics"]
    assert mt["duplicate_rate"] == pytest.approx(0.2)
    assert mt["false_merge_rate"] == 0.0
    # gold pairs 5 (drill 3, wa 1, wb 1); predicted pairs 3 (D1 1, WA 1, WB 1) all correct -> P 1, R 3/5
    assert mt["pairwise_precision"] == pytest.approx(1.0) and mt["pairwise_recall"] == pytest.approx(0.6)
    assert mt["location_acc"] == pytest.approx(1.0)
    assert mt["event_recall"] == 0.0 and mt["event_precision"] == 0.0


def test_ac_eval_03_score_merged_twins(data):
    """identical twins merged: false_merge_rate = 2/5; pairwise P = 5/9, R = 1."""
    groups = {"D": [("drill-1", 0), ("drill-1", 1), ("drill-1", 2)],
              "W": [("wrench-a", 0), ("wrench-a", 1), ("wrench-b", 0), ("wrench-b", 1)],
              "U": [("unk-1", 0)], "L": [("laptop-1", 0)]}
    catalog = {"D": "drill-pistol", "W": "wrench", "U": None, "L": "laptop"}
    mt = score(data, build_snapshot(groups, catalog=catalog))["metrics"]
    assert mt["false_merge_rate"] == pytest.approx(0.4)
    assert mt["duplicate_rate"] == 0.0
    assert mt["pairwise_precision"] == pytest.approx(5 / 9) and mt["pairwise_recall"] == pytest.approx(1.0)
    assert mt["pairwise_f1"] == pytest.approx(2 * (5 / 9) / (1 + 5 / 9))


def test_ac_eval_03_score_unknown_recall_needs_unknown_object_review(data):
    snap = perfect_snapshot()
    snap.reviews = [PredReview("possible_duplicate", "E-unk-1")]
    assert score(data, snap)["metrics"]["unknown_recall"] == 0.0
    snap.reviews = [PredReview("unknown_object", "E-unk-1", status="resolved")]
    assert score(data, snap)["metrics"]["unknown_recall"] == 1.0, "a resolved review still reached the queue"


def test_ac_eval_03_score_event_timing_and_actor(data):
    snap = perfect_snapshot()
    snap.events = [PredEvent("moved", "E-drill-1", at(12.0), "crew member, blue shirt", "LAB1P3"),   # dt 4 ok
                   PredEvent("removed", "E-laptop-1", at(30.0), "crew member, red shirt", None)]    # dt 24 -> FP
    mt = score(data, snap)["metrics"]
    assert mt["event_precision"] == pytest.approx(0.5) and mt["event_recall"] == pytest.approx(0.5)
    assert mt["actor_accuracy"] == pytest.approx(1.0)
    assert score(data, snap, event_tolerance_s=3.0)["metrics"]["event_recall"] == 0.0


def test_ac_eval_03_score_wrong_location(data):
    snap = perfect_snapshot()
    e = snap.entities["E-drill-1"]
    snap.entities["E-drill-1"] = PredEntity(e.entity_id, e.catalog_id, e.status, last_location="LAB1",
                                            last_seen_at=e.last_seen_at)
    mt = score(data, snap)["metrics"]
    # zone wrong for 1 of 4 (review round: laptop-1 was removed at t=6, so it is out of scope at the audit time)
    assert mt["location_acc"] == pytest.approx(0.75)
    assert mt["location_module_acc"] == pytest.approx(1.0)  # LAB1 is the right module


def test_ac_eval_03_score_audit_time_override(data):
    # review round: at audit_t = 5 the predicted location comes from the pipeline's sightings/events up to t = 5
    # (drill seen in LAB1S2), not from its FINAL state (LAB1P3). A perfect pipeline is perfect at any audit time.
    res = score(data, perfect_snapshot(), audit_t=5)
    assert res["metrics"]["location_acc"] == pytest.approx(1.0)
    assert res["counts"]["location_scored"] == 5          # laptop not removed yet at t = 5
    snap = perfect_snapshot()
    snap.sightings.append(PredSighting("bad", "E-drill-1", at(3), "LAB1O3", (0.1, 0.1, 0.2, 0.2)))
    assert score(data, snap, audit_t=5)["metrics"]["location_acc"] == pytest.approx(0.8)


def test_ac_eval_03_score_removed_object_out_of_scope(data):
    """laptop-1 is removed at t=6: it is excluded from the default (t=10) location denominator."""
    res = score(data, perfect_snapshot())
    assert res["counts"]["location_scored"] == 4 and res["metrics"]["location_acc"] == pytest.approx(1.0)


def test_ac_eval_03_score_predicted_removal_too_early_is_wrong(data):
    snap = perfect_snapshot()
    snap.events.append(PredEvent("removed", "E-laptop-1", at(3), "crew member, red shirt", None))
    assert score(data, snap, audit_t=5)["metrics"]["location_acc"] == pytest.approx(0.8)


def test_ac_eval_03_score_empty_run(data):
    mt = score(data, Snapshot(locations=dict(SEED_LOCATIONS)))["metrics"]
    assert mt["unknown_recall"] == 0.0 and mt["location_acc"] == 0.0 and mt["detection_macro_f1"] == 0.0
    assert mt["duplicate_rate"] == 0.0
    # review round: an empty run must not look perfect on entity resolution / coverage
    assert mt["pairwise_f1"] == 0.0 and mt["pairwise_recall"] == 0.0
    assert mt["bcubed_f1"] < 1.0
    assert mt["sighting_recall"] == 0.0 and mt["object_recall"] == 0.0
    assert all(not (isinstance(v, float) and math.isnan(v)) for v in mt.values() if v is not None)
    assert not check(mt, load_thresholds(EVAL_DIR / "thresholds.yaml")).passed


def test_ac_eval_03_score_same_frame_duplicate_entities(data):
    """Two entities sighted at the same t on the same bbox (central dedup failure): both sightings count."""
    snap = perfect_snapshot()
    snap.entities["E-dup"] = PredEntity("E-dup", "drill-pistol", "provisional", last_location="LAB1S2",
                                        last_seen_at=at(1))
    snap.sightings.append(PredSighting("dup", "E-dup", at(1), "LAB1S2", (0.10, 0.10, 0.20, 0.20)))
    res = score(data, snap)
    assert res["metrics"]["duplicate_rate"] == pytest.approx(0.2)
    assert res["metrics"]["pairwise_recall"] < 1.0
    assert res["entity_to_gold"]["E-dup"] == "drill-1"


def test_ac_eval_03_score_spurious_entity_counted(data):
    snap = perfect_snapshot()
    snap.entities["E-ghost"] = PredEntity("E-ghost", "wrench", "provisional")
    snap.sightings.append(PredSighting("ghost", "E-ghost", at(4), "LAB1S2", (0.90, 0.90, 0.99, 0.99)))
    res = score(data, snap)
    assert res["metrics"]["spurious_entity_rate"] == pytest.approx(1 / 6)
    assert res["counts"]["spurious_entities"] == 1


# ---------------------------------------------------------------- synthetic scene (seed 7): gate must not pass trivially
@pytest.fixture(scope="module")
def synth7(tmp_path_factory):
    from nasa_eval.synth import SceneConfig, generate_scene, write_scene

    scene = generate_scene(7, SceneConfig(width=160, height=90))
    return load_dataset(write_scene(scene, tmp_path_factory.mktemp("synth"), "synth-7"))


def _synth_snapshot(ds, *, every: int = 1, first_only: bool = False, offset_s: float = 0.0) -> Snapshot:
    """One entity per gold object; sightings for every ``every``-th appearance (or only the first one)."""
    snap = Snapshot(locations=dict(SEED_LOCATIONS))
    n = 0
    for g in ds.gold:
        eid = "E-" + g.gold_object_id
        apps = g.appearances[:1] if first_only else g.appearances[::every]
        for a in apps:
            n += 1
            snap.sightings.append(PredSighting(f"s{n}", eid, at(a.t + offset_s), a.location, a.bbox))
        last = apps[-1]
        snap.entities[eid] = PredEntity(eid, g.catalog_id, "unknown" if g.is_unknown else "provisional",
                                        last_location=last.location, last_seen_at=at(last.t + offset_s))
        for e in g.events:
            snap.events.append(PredEvent(e.type, eid, at(e.t), e.actor, e.to_location))
        if g.is_unknown:
            snap.reviews.append(PredReview("unknown_object", eid))
    return snap


def _gate(mt):
    return check(mt, load_thresholds(EVAL_DIR / "thresholds.yaml"))


def test_ac_eval_03_synth_empty_snapshot_fails_repo_gate(synth7):
    mt = score(synth7, Snapshot(locations=dict(SEED_LOCATIONS)))["metrics"]
    assert mt["pairwise_f1"] == 0.0
    assert not _gate(mt).passed


def test_ac_eval_03_synth_one_sighting_per_object_fails_repo_gate(synth7):
    mt = score(synth7, _synth_snapshot(synth7, first_only=True))["metrics"]
    assert mt["pairwise_f1"] < 0.1 and mt["sighting_recall"] < 0.3
    failed = {f.metric for f in _gate(mt).failures}
    assert {"pairwise_f1", "sighting_recall"} <= failed


def test_ac_eval_03_synth_perfect_per_frame_passes_repo_gate(synth7):
    mt = score(synth7, _synth_snapshot(synth7))["metrics"]
    assert mt["pairwise_f1"] == pytest.approx(1.0) and mt["sighting_recall"] == pytest.approx(1.0)
    assert _gate(mt).passed, _gate(mt).failures


def test_ac_eval_03_synth_per_window_sightings_match_with_window_tolerance(synth7):
    """Perception stamps sightings per window (6 frames at 1 fps every 4 s, AC-PER-2): with the default tolerance
    (one window span, 5 s) a correct per-window pipeline covers every appearance; a per-frame tolerance would call
    most of them missed."""
    snap = _synth_snapshot(synth7, every=4, offset_s=1.0)
    assert DEFAULT_MATCH_TOLERANCE_S == pytest.approx(5.0)
    mt = score(synth7, snap)["metrics"]
    assert mt["sighting_recall"] == pytest.approx(1.0) and mt["pairwise_f1"] == pytest.approx(1.0)
    strict = score(synth7, snap, match_time_tolerance_s=0.5)["metrics"]
    assert strict["sighting_recall"] < 0.5


# ---------------------------------------------------------------- read_snapshot from Postgres
@pytest.mark.db
def test_ac_eval_03_read_snapshot_from_db(db_writer):
    ids = db_writer.perfect_replay_of(TINY_GOLD)
    snap = read_snapshot(db_writer.url)
    assert set(snap.entities) == set(ids.values())
    assert len(snap.sightings) == sum(len(g["appearances"]) for g in TINY_GOLD)
    drill = snap.entities[ids["drill-1"]]
    assert drill.catalog_id == "drill-pistol" and drill.last_location == "LAB1P3"
    s = next(x for x in snap.sightings if x.entity_id == ids["drill-1"] and x.seen_at == at(10))
    assert s.location_id == "LAB1P3" and s.bbox == pytest.approx((0.6, 0.6, 0.7, 0.7))
    assert any(e.event_type == "moved" and e.entity_id == ids["drill-1"] and e.actor_desc == "crew member, blue shirt"
               and e.to_location == "LAB1P3" for e in snap.events)
    assert [(r.kind, r.entity_id) for r in snap.reviews] == [("unknown_object", ids["unk-1"])]
    assert snap.locations["LAB1S2"] == ("LAB1", "zone") and snap.locations["LAB1"] == ("USOS", "module")


@pytest.mark.db
def test_ac_eval_03_read_snapshot_resolves_merged_entities(db_writer):
    target = db_writer.entity("wrench", last_location="LAB1S2", last_seen_s=3)
    dup = db_writer.entity("wrench", status="merged", merged_into=target)
    db_writer.sighting(dup, 2, "LAB1S2", [0.3, 0.1, 0.4, 0.2])
    db_writer.sighting(target, 3, "LAB1S2", [0.3, 0.1, 0.4, 0.2])
    db_writer.event(dup, "moved", 2, "crew member", "LAB1S2")
    snap = read_snapshot(db_writer.url)
    assert set(snap.entities) == {target}, "merged entities are not live"
    assert {s.entity_id for s in snap.sightings} == {target}
    assert [e.entity_id for e in snap.events] == [target]


@pytest.mark.db
def test_ac_eval_03_read_snapshot_then_score_perfect(db_writer, data):
    db_writer.perfect_replay_of(TINY_GOLD)
    mt = score(data, read_snapshot(db_writer.url))["metrics"]
    assert mt["pairwise_f1"] == pytest.approx(1.0) and mt["duplicate_rate"] == 0.0
    assert mt["unknown_recall"] == 1.0 and mt["location_acc"] == pytest.approx(1.0)
