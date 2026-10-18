"""AC-PER-8 Reconciler: omni objects linked to detector boxes (IoU or label); candidates from /ingest/candidates; hard
gates (class mismatch, impossible transit, one entity per window); p = f(similarity, class, zone, recency); bands
(>= 0.85 assign, 0.5..0.85 decision model + 0.6 acceptance, < 0.5 new provisional); expected_count ->
possible_duplicate; UNKNOWN -> unknown entity + unknown_object review with crop, description and timestamp."""
from __future__ import annotations

from datetime import timedelta

import numpy as np
import pytest
from support import (
    BUCKET,
    T0,
    FakeInventory,
    assignment_for_bbox,
    blend,
    cosine,
    detections_for,
    iso,
    obj,
    observation,
    parse_dt,
    random_unit,
    scene_window,
    uri_key,
)

from perception import NEW
from perception.fakes import FakeDecision, FakeEmbedder
from perception.reconciler import CandidateFeatures, band, iou, score_candidate
from perception.types import DecisionResult, OmniResult

BOX_A = (0.10, 0.10, 0.30, 0.40)
BOX_B = (0.55, 0.50, 0.85, 0.80)
VEC = FakeEmbedder()


def vec(object_id: str) -> np.ndarray:
    return VEC.vector_for_object(object_id)


def process(rec, frames_objects, objects, labels, *, people=(), events=(), location_hint=None, step_s=1.0,
            t0=T0, transcript=""):
    w = scene_window(frames_objects, t0=t0, step_s=step_s)
    omni = OmniResult(status="ok", observation=observation(objects, people=people, events=events,
                                                           location_hint=location_hint, transcript=transcript),
                      model="fake-omni", raw="{}")
    return rec.process(w, omni, detections_for(frames_objects, labels)), w


def option_keys(options) -> list[str]:
    return list(options.keys()) if isinstance(options, dict) else list(options)


def const(p):
    return lambda features: p


# ---------------------------------------------------------------------------------------------- scoring + bands
@pytest.mark.parametrize("p,expected", [(1.0, "assign"), (0.85, "assign"), (0.8499, "ask"), (0.7, "ask"),
                                        (0.5, "ask"), (0.4999, "new"), (0.0, "new")])
def test_ac_per_08_band_boundaries(p, expected):
    assert band(p) == expected


def test_ac_per_08_band_custom_thresholds():
    assert band(0.8, assign_p=0.9, ask_p=0.6) == "ask"
    assert band(0.55, assign_p=0.9, ask_p=0.6) == "new"


def test_ac_per_08_score_strong_match_assigns_weak_match_is_new():
    strong = score_candidate(CandidateFeatures(similarity=0.98, class_match=True, zone_match=True, recency_s=30))
    weak = score_candidate(CandidateFeatures(similarity=0.05, class_match=True, zone_match=False, recency_s=86400))
    assert strong >= 0.85
    assert weak < 0.5


def test_ac_per_08_score_moved_object_is_at_least_ask_band():
    """Same appearance and class but a different zone a minute later (an object that was moved) must not be
    created as new outright: it lands at least in the decision-model band."""
    p = score_candidate(CandidateFeatures(similarity=0.98, class_match=True, zone_match=False, recency_s=60))
    assert p >= 0.5


def test_ac_per_08_score_in_unit_interval():
    for sim in (-1.0, 0.0, 0.5, 1.0):
        for cm in (True, False):
            for zm in (True, False):
                for rec in (None, 0.0, 60.0, 1e6):
                    p = score_candidate(CandidateFeatures(sim, cm, zm, rec))
                    assert 0.0 <= p <= 1.0


def test_ac_per_08_score_monotonic_in_each_factor():
    base = {"similarity": 0.7, "class_match": True, "zone_match": False, "recency_s": 600.0}
    p0 = score_candidate(CandidateFeatures(**base))
    assert score_candidate(CandidateFeatures(**{**base, "similarity": 0.9})) > p0
    assert score_candidate(CandidateFeatures(**{**base, "zone_match": True})) > p0
    assert score_candidate(CandidateFeatures(**{**base, "class_match": False})) < p0
    assert score_candidate(CandidateFeatures(**{**base, "recency_s": 10.0})) >= p0
    assert score_candidate(CandidateFeatures(**{**base, "recency_s": 7 * 86400.0})) <= p0


def test_ac_per_08_iou():
    assert iou((0, 0, 1, 1), (0, 0, 1, 1)) == pytest.approx(1.0)
    assert iou((0, 0, 0.5, 0.5), (0.5, 0.5, 1, 1)) == pytest.approx(0.0)
    assert iou((0, 0, 0.5, 1), (0.25, 0, 0.75, 1)) == pytest.approx(1 / 3)
    assert iou((0.2, 0.2, 0.2, 0.2), (0, 0, 1, 1)) == 0.0


# ---------------------------------------------------------------------------------------------- step 1: candidates
def test_ac_per_08_candidates_requested_with_crop_embedding_and_class(make_reconciler, inv):
    process(make_reconciler(), [[("laptop-A", BOX_A)]], [obj("laptop", BOX_A)], {"laptop-A": "laptop"})
    assert inv.candidate_calls, "candidates must be retrieved before deciding"
    call = inv.candidate_calls[0]
    assert call["catalog_id"] == "laptop"
    assert cosine(call["embedding"], vec("laptop-A")) > 0.999


def test_ac_per_08_new_entity_carries_exemplar_and_sighting(make_reconciler, inv):
    outcome, w = process(make_reconciler(), [[("laptop-A", BOX_A)]], [obj("laptop", BOX_A, confidence=0.8)],
                         {"laptop-A": "laptop"})
    assert outcome.status == "posted"
    a = inv.last["assignments"][0]
    assert a.get("entity_id") is None
    assert a["new_entity"]["catalog_id"] == "laptop"
    assert a["new_entity"]["status"] == "provisional"
    assert a["new_entity"]["kind"] == "object"
    assert cosine(a["exemplar"]["embedding"], vec("laptop-A")) > 0.999
    s = a["sighting"]
    assert parse_dt(s["seen_at"]) == w.frames[0].timestamp
    assert 0.0 <= s["confidence"] <= 1.0
    assert s["location_id"] == "LAB1"
    assert all(abs(x - y) < 0.02 for x, y in zip(s["bbox"], BOX_A, strict=True))


def test_ac_per_08_window_fields(make_reconciler, inv):
    _, w = process(make_reconciler(), [[("laptop-A", BOX_A)], [("laptop-A", BOX_A)]], [obj("laptop", BOX_A)],
                   {"laptop-A": "laptop"})
    win = inv.last["window"]
    assert win["camera_id"] == "cam-lab1-fwd"
    assert parse_dt(win["t_start"]) == w.t_start and parse_dt(win["t_end"]) == w.t_end
    assert win["model"]
    assert win["scene_summary"] == "crew working in the module"


# ---------------------------------------------------------------------------------------------- step 4: bands
def test_ac_per_08_high_score_assigns_existing_entity_without_decision(make_reconciler, inv):
    eid = inv.add_entity(catalog_id="laptop", embedding=vec("laptop-A"), last_location="LAB1",
                         last_seen_at=T0 - timedelta(seconds=60))
    decision = FakeDecision()
    process(make_reconciler(decision=decision), [[("laptop-A", BOX_A)]], [obj("laptop", BOX_A)],
            {"laptop-A": "laptop"})
    a = inv.last["assignments"][0]
    assert str(a["entity_id"]) == eid
    assert not a.get("new_entity")
    assert decision.calls == []
    assert inv.last.get("decisions", []) == []


def test_ac_per_08_low_score_creates_new_provisional_without_decision(make_reconciler, inv):
    inv.add_entity(catalog_id="laptop", embedding=random_unit(42), last_location="LAB1",
                   last_seen_at=T0 - timedelta(days=3))
    decision = FakeDecision()
    process(make_reconciler(decision=decision), [[("laptop-A", BOX_A)]], [obj("laptop", BOX_A)],
            {"laptop-A": "laptop"})
    a = inv.last["assignments"][0]
    assert a["new_entity"]["status"] == "provisional"
    assert decision.calls == []
    assert [r for r in inv.last.get("reviews", []) if r["kind"] == "possible_duplicate"] == []


def test_ac_per_08_no_candidates_creates_new_provisional(make_reconciler, inv):
    process(make_reconciler(), [[("laptop-A", BOX_A)]], [obj("laptop", BOX_A)], {"laptop-A": "laptop"})
    assert inv.last["assignments"][0]["new_entity"]["status"] == "provisional"


def test_ac_per_08_middle_band_asks_decision_and_assigns_when_confident(make_reconciler, inv):
    eid = inv.add_entity(catalog_id="laptop", embedding=blend(vec("laptop-A"), random_unit(3), 0.7),
                         last_location="LAB1", last_seen_at=T0 - timedelta(hours=1))
    seen = []

    def answer(question, options, state):
        seen.append((question, option_keys(options), state))
        return DecisionResult(choice=eid, probabilities={eid: 0.8, NEW: 0.2}, latency_ms=12, model="fake-decision")

    rec = make_reconciler(decision=FakeDecision(answer), scorer=const(0.7))
    process(rec, [[("laptop-A", BOX_A)]], [obj("laptop", BOX_A)], {"laptop-A": "laptop"})
    assert len(seen) == 1
    question, keys, _ = seen[0]
    assert question == "same_or_new"
    assert eid in keys and NEW in keys
    a = inv.last["assignments"][0]
    assert str(a["entity_id"]) == eid
    d = inv.last["decisions"]
    assert len(d) == 1
    assert d[0]["question"] == "same_or_new" and d[0]["choice"] == eid
    assert d[0]["probabilities"][eid] == pytest.approx(0.8)
    assert d[0]["model"] == "fake-decision" and d[0]["latency_ms"] == 12
    assert isinstance(d[0]["subject"], dict)


def test_ac_per_08_middle_band_low_decision_confidence_creates_provisional_plus_duplicate_review(make_reconciler,
                                                                                                    inv):
    eid = inv.add_entity(catalog_id="laptop", embedding=blend(vec("laptop-A"), random_unit(3), 0.7),
                         last_location="LAB1", last_seen_at=T0 - timedelta(hours=1))
    dec = FakeDecision(lambda q, o, s: {"choice": eid, "probabilities": {eid: 0.55, NEW: 0.45}})
    _, w = process(make_reconciler(decision=dec, scorer=const(0.7)), [[("laptop-A", BOX_A)]],
                   [obj("laptop", BOX_A, description="grey laptop with stickers")], {"laptop-A": "laptop"})
    p = inv.last
    a = p["assignments"][0]
    assert a["new_entity"]["status"] == "provisional"
    reviews = [r for r in p["reviews"] if r["kind"] == "possible_duplicate"]
    assert len(reviews) == 1
    r = reviews[0]
    assert eid in [str(c) for c in r["candidate_ids"]]
    assert r["entity_ref"] == a["ref"]
    assert r["image_uri"].startswith(f"s3://{BUCKET}/")
    assert r["description"]
    assert parse_dt(r["observed_at"]) == w.frames[0].timestamp
    assert r["probabilities"].get(eid) == pytest.approx(0.55)


def test_ac_per_08_decision_exactly_at_acceptance_assigns(make_reconciler, inv):
    eid = inv.add_entity(catalog_id="laptop", embedding=vec("laptop-A"), last_location="LAB1",
                         last_seen_at=T0 - timedelta(hours=1))
    dec = FakeDecision(lambda q, o, s: {"choice": eid, "probabilities": {eid: 0.6, NEW: 0.4}})
    process(make_reconciler(decision=dec, scorer=const(0.6)), [[("laptop-A", BOX_A)]], [obj("laptop", BOX_A)],
            {"laptop-A": "laptop"})
    assert str(inv.last["assignments"][0]["entity_id"]) == eid


def test_ac_per_08_decision_choosing_new_creates_new_entity(make_reconciler, inv):
    eid = inv.add_entity(catalog_id="laptop", embedding=vec("laptop-A"), last_location="LAB1",
                         last_seen_at=T0 - timedelta(hours=1))
    dec = FakeDecision(lambda q, o, s: {"choice": NEW, "probabilities": {eid: 0.1, NEW: 0.9}})
    process(make_reconciler(decision=dec, scorer=const(0.7)), [[("laptop-A", BOX_A)]], [obj("laptop", BOX_A)],
            {"laptop-A": "laptop"})
    a = inv.last["assignments"][0]
    assert a.get("entity_id") is None and a["new_entity"]["status"] == "provisional"


def test_ac_per_08_decision_failure_creates_new_and_marks_for_review(make_reconciler, inv):
    eid = inv.add_entity(catalog_id="laptop", embedding=vec("laptop-A"), last_location="LAB1",
                         last_seen_at=T0 - timedelta(hours=1))
    dec = FakeDecision(lambda q, o, s: TimeoutError("decision model timed out"))
    process(make_reconciler(decision=dec, scorer=const(0.7)), [[("laptop-A", BOX_A)]], [obj("laptop", BOX_A)],
            {"laptop-A": "laptop"})
    p = inv.last
    a = p["assignments"][0]
    assert a.get("entity_id") is None
    reviews = [r for r in p["reviews"] if r.get("entity_ref") == a["ref"]]
    assert reviews and reviews[0]["kind"] in ("possible_duplicate", "low_confidence")
    assert eid in [str(c) for c in reviews[0]["candidate_ids"]]
    assert p["decisions"][0]["choice"] == NEW and p["decisions"][0]["probabilities"] == {}


# ---------------------------------------------------------------------------------------------- step 2: gates
def test_ac_per_08_gate_class_mismatch_never_assigns(make_reconciler):
    api = FakeInventory(ignore_catalog_filter=True)
    wrench = api.add_entity(catalog_id="wrench", embedding=vec("drill-1"), last_location="LAB1",
                            last_seen_at=T0 - timedelta(hours=1))
    dec = FakeDecision(lambda q, o, s: {"choice": wrench, "probabilities": {wrench: 0.99}})
    process(make_reconciler(api=api, decision=dec, scorer=const(0.99)), [[("drill-1", BOX_A)]],
            [obj("drill", BOX_A)], {"drill-1": "drill"})
    a = api.last["assignments"][0]
    assert str(a.get("entity_id")) != wrench
    assert a["new_entity"]["catalog_id"] == "drill-pistol"


def test_ac_per_08_gate_impossible_transit_blocks_non_held_object(make_reconciler, inv):
    eid = inv.add_entity(catalog_id="drill-pistol", embedding=vec("drill-1"), last_location="LAB1",
                         last_seen_at=T0 - timedelta(seconds=5))
    process(make_reconciler(camera_id="cam-nod2-aft", scorer=const(0.95)), [[("drill-1", BOX_A)]],
            [obj("drill", BOX_A)], {"drill-1": "drill"})
    a = inv.last["assignments"][0]
    assert str(a.get("entity_id")) != eid
    assert a["new_entity"]["catalog_id"] == "drill-pistol"


def test_ac_per_08_gate_transit_allows_held_object(make_reconciler, inv):
    eid = inv.add_entity(catalog_id="drill-pistol", embedding=vec("drill-1"), last_location="LAB1",
                         last_seen_at=T0 - timedelta(seconds=5))
    process(make_reconciler(camera_id="cam-nod2-aft", scorer=const(0.95)), [[("drill-1", BOX_A)]],
            [obj("drill", BOX_A, held_by="P1")], {"drill-1": "drill"},
            people=[{"ref": "P1", "appearance": "blue shirt"}])
    assert str(inv.last["assignments"][0]["entity_id"]) == eid


def test_ac_per_08_gate_transit_allows_after_min_transit(make_reconciler, inv):
    eid = inv.add_entity(catalog_id="drill-pistol", embedding=vec("drill-1"), last_location="LAB1",
                         last_seen_at=T0 - timedelta(seconds=25))
    process(make_reconciler(camera_id="cam-nod2-aft", scorer=const(0.95)), [[("drill-1", BOX_A)]],
            [obj("drill", BOX_A)], {"drill-1": "drill"})
    assert str(inv.last["assignments"][0]["entity_id"]) == eid


def test_ac_per_08_gate_transit_same_module_different_zone_is_allowed(make_reconciler, inv):
    eid = inv.add_entity(catalog_id="drill-pistol", embedding=vec("drill-1"), last_location="NOD2D4",
                         last_seen_at=T0 - timedelta(seconds=5))
    process(make_reconciler(camera_id="cam-nod2-aft", scorer=const(0.95)), [[("drill-1", BOX_A)]],
            [obj("drill", BOX_A)], {"drill-1": "drill"})
    assert str(inv.last["assignments"][0]["entity_id"]) == eid


def test_ac_per_08_gate_transit_respects_configured_min_transit(make_reconciler, inv):
    eid = inv.add_entity(catalog_id="drill-pistol", embedding=vec("drill-1"), last_location="LAB1",
                         last_seen_at=T0 - timedelta(seconds=25))
    process(make_reconciler(camera_id="cam-nod2-aft", scorer=const(0.95), min_transit_s=60.0),
            [[("drill-1", BOX_A)]], [obj("drill", BOX_A)], {"drill-1": "drill"})
    assert str(inv.last["assignments"][0].get("entity_id")) != eid


def test_ac_per_08_gate_same_entity_twice_in_window_only_best_keeps_it(make_reconciler, inv):
    # E is closer to laptop-A (0.8) than to laptop-B (0.6); both would be "assign" under this scorer.
    va, vb = vec("laptop-A"), vec("laptop-B")
    e = inv.add_entity(catalog_id="laptop", embedding=(0.8 * va + 0.6 * (vb - np.dot(vb, va) * va)
                                                       / np.linalg.norm(vb - np.dot(vb, va) * va)),
                       last_location="LAB1", last_seen_at=T0 - timedelta(hours=1))
    rec = make_reconciler(scorer=lambda f: min(1.0, f.similarity + 0.3))
    process(rec, [[("laptop-A", BOX_A), ("laptop-B", BOX_B)]], [obj("laptop", BOX_A), obj("laptop", BOX_B)],
            {"laptop-A": "laptop", "laptop-B": "laptop"})
    p = inv.last
    assert len(p["assignments"]) == 2
    to_e = [a for a in p["assignments"] if str(a.get("entity_id")) == e]
    assert len(to_e) == 1, "an entity may be matched only once per window"
    assert to_e[0] is assignment_for_bbox(p, BOX_A)
    assert assignment_for_bbox(p, BOX_B)["new_entity"]["catalog_id"] == "laptop"


# ---------------------------------------------------------------------------------------------- step 5: expected_count
def test_ac_per_08_expected_count_exceeded_flags_possible_duplicate(make_reconciler, inv):
    inv.add_entity(catalog_id="multimeter", embedding=random_unit(7), last_location="LAB1",
                   last_seen_at=T0 - timedelta(days=2))  # expected_count = 1 already used
    process(make_reconciler(), [[("mm-2", BOX_A)]], [obj("multimeter", BOX_A)], {"mm-2": "multimeter"})
    p = inv.last
    a = p["assignments"][0]
    assert a["new_entity"]["catalog_id"] == "multimeter"
    dup = [r for r in p["reviews"] if r["kind"] == "possible_duplicate"]
    assert len(dup) == 1 and dup[0]["entity_ref"] == a["ref"]


def test_ac_per_08_expected_count_not_exceeded_no_review(make_reconciler, inv):
    process(make_reconciler(), [[("mm-1", BOX_A)]], [obj("multimeter", BOX_A)], {"mm-1": "multimeter"})
    assert [r for r in inv.last.get("reviews", []) if r["kind"] == "possible_duplicate"] == []


def test_ac_per_08_expected_count_counts_new_entities_in_same_window(make_reconciler, inv):
    process(make_reconciler(), [[("mm-1", BOX_A), ("mm-2", BOX_B)]],
            [obj("multimeter", BOX_A), obj("multimeter", BOX_B)], {"mm-1": "multimeter", "mm-2": "multimeter"})
    dup = [r for r in inv.last["reviews"] if r["kind"] == "possible_duplicate"]
    assert len(dup) >= 1


def test_ac_per_08_merged_entities_do_not_count_toward_expected_count(make_reconciler, inv):
    live = inv.add_entity(catalog_id="multimeter", embedding=random_unit(8), last_location="LAB1",
                          last_seen_at=T0 - timedelta(days=2))
    inv.entities[live]["status"] = "merged"
    process(make_reconciler(), [[("mm-2", BOX_A)]], [obj("multimeter", BOX_A)], {"mm-2": "multimeter"})
    assert [r for r in inv.last.get("reviews", []) if r["kind"] == "possible_duplicate"] == []


def test_ac_per_08_null_expected_count_never_flags(make_reconciler, inv):
    process(make_reconciler(), [[("bag-1", BOX_A), ("bag-2", BOX_B)]], [obj("m-bag", BOX_A), obj("m-bag", BOX_B)],
            {"bag-1": "m-bag", "bag-2": "m-bag"})
    assert inv.last.get("reviews", []) == []


def test_ac_per_08_assigning_existing_entity_does_not_trigger_expected_count(make_reconciler, inv):
    eid = inv.add_entity(catalog_id="multimeter", embedding=vec("mm-1"), last_location="LAB1",
                         last_seen_at=T0 - timedelta(minutes=5))
    process(make_reconciler(), [[("mm-1", BOX_A)]], [obj("multimeter", BOX_A)], {"mm-1": "multimeter"})
    assert str(inv.last["assignments"][0]["entity_id"]) == eid
    assert inv.last.get("reviews", []) == []


# ---------------------------------------------------------------------------------------------- step 6: UNKNOWN
def test_ac_per_08_unknown_class_creates_unknown_entity_and_review(make_reconciler, inv, storage):
    _outcome, w = process(make_reconciler(), [[("orb-1", BOX_A)], [("orb-1", BOX_A)]],
                         [obj("glowing orb", BOX_A, frame_index=1, description="small glowing sphere with a cable")],
                         {"orb-1": "glowing orb"})
    p = inv.last
    a = p["assignments"][0]
    assert a["new_entity"]["status"] == "unknown"
    assert a["new_entity"]["catalog_id"] is None
    assert a["new_entity"]["label"]
    reviews = [r for r in p["reviews"] if r["kind"] == "unknown_object"]
    assert len(reviews) == 1
    r = reviews[0]
    assert r["entity_ref"] == a["ref"]
    assert "glowing sphere" in r["description"]
    assert parse_dt(r["observed_at"]) == w.frames[1].timestamp
    key = uri_key(r["image_uri"])
    assert key.startswith("crops/cam-lab1-fwd/")
    assert storage.objects[key][:2] == b"\xff\xd8"


def test_ac_per_08_unknown_resighted_matches_existing_without_new_review(make_reconciler, inv):
    rec = make_reconciler()
    process(rec, [[("orb-1", BOX_A)]], [obj("glowing orb", BOX_A)], {"orb-1": "glowing orb"})
    first_ref = inv.last["assignments"][0]["ref"]
    unknown_id = next(e["entity_id"] for e in inv.entities.values() if e["status"] == "unknown")
    assert first_ref
    process(rec, [[("orb-1", BOX_A)]], [obj("strange orb", BOX_A)], {"orb-1": "strange orb"},
            t0=T0 + timedelta(seconds=30))
    second = inv.last
    assert str(second["assignments"][0]["entity_id"]) == unknown_id
    assert len([r for r in inv.reviews if r["kind"] == "unknown_object"]) == 1


# ---------------------------------------------------------------------------------------------- linking
def test_ac_per_08_omni_object_linked_to_detector_box_by_iou(make_reconciler, inv):
    omni_box = (0.12, 0.11, 0.31, 0.42)  # IoU with BOX_A ~0.85
    process(make_reconciler(), [[("laptop-A", BOX_A)]], [obj("laptop", omni_box)], {"laptop-A": "laptop"})
    a = inv.last["assignments"][0]
    assert all(abs(x - y) < 1e-3 for x, y in zip(a["sighting"]["bbox"], BOX_A, strict=True)), "detector box wins"
    assert cosine(a["exemplar"]["embedding"], vec("laptop-A")) > 0.999


def test_ac_per_08_omni_object_without_bbox_linked_by_label(make_reconciler, inv):
    process(make_reconciler(), [[("drill-1", BOX_A)]], [obj("drill", None, frame_index=None)], {"drill-1": "PGT"})
    a = inv.last["assignments"][0]
    assert a["new_entity"]["catalog_id"] == "drill-pistol"
    assert all(abs(x - y) < 1e-3 for x, y in zip(a["sighting"]["bbox"], BOX_A, strict=True))
    assert cosine(a["exemplar"]["embedding"], vec("drill-1")) > 0.999


# ---------------------------------------------------------------------------------------------- bbox_source (UI2-07)
def test_ac_per_08_bbox_source_detector_when_linked_to_a_detector_box(make_reconciler, inv):
    omni_box = (0.12, 0.11, 0.31, 0.42)  # linked to BOX_A by IoU: the sighting carries the detector's box
    process(make_reconciler(), [[("laptop-A", BOX_A)]], [obj("laptop", omni_box)], {"laptop-A": "laptop"})
    s = inv.last["assignments"][0]["sighting"]
    assert s["bbox_source"] == "detector"


def test_ac_per_08_bbox_source_detector_when_linked_by_label(make_reconciler, inv):
    process(make_reconciler(), [[("drill-1", BOX_A)]], [obj("drill", None, frame_index=None)], {"drill-1": "PGT"})
    s = inv.last["assignments"][0]["sighting"]
    assert s["bbox"] is not None and s["bbox_source"] == "detector"


def test_ac_per_08_bbox_source_omni_without_a_detector_box(make_reconciler, inv):
    process(make_reconciler(), [[]], [obj("laptop", BOX_B)], {})
    s = inv.last["assignments"][0]["sighting"]
    assert all(abs(x - y) < 1e-3 for x, y in zip(s["bbox"], BOX_B, strict=True))
    assert s["bbox_source"] == "omni"


def test_ac_per_08_bbox_source_null_without_any_box(make_reconciler, inv):
    process(make_reconciler(), [[]], [obj("laptop", None, frame_index=None)], {})
    s = inv.last["assignments"][0]["sighting"]
    assert s["bbox"] is None and s["bbox_source"] is None


def test_ac_per_08_one_assignment_per_observed_object(make_reconciler, inv):
    process(make_reconciler(), [[("laptop-A", BOX_A), ("drill-1", BOX_B)]],
            [obj("laptop", BOX_A), obj("drill", BOX_B)], {"laptop-A": "laptop", "drill-1": "drill"})
    cats = sorted(a["new_entity"]["catalog_id"] for a in inv.last["assignments"])
    assert cats == ["drill-pistol", "laptop"]
    refs = [a["ref"] for a in inv.last["assignments"]]
    assert len(set(refs)) == 2 and all(refs)


# ---------------------------------------------------------------------------------------------- robustness
def test_ac_per_08_invalid_omni_result_is_never_written(make_reconciler, inv, storage):
    w = scene_window([[("laptop-A", BOX_A)]])
    out = make_reconciler().process(w, OmniResult(status="invalid", raw="garbage", model="m"),
                                    detections_for([[("laptop-A", BOX_A)]], {"laptop-A": "laptop"}))
    assert out.status == "invalid"
    assert inv.posted == []


def test_ac_per_08_error_omni_result_is_never_written(make_reconciler, inv):
    w = scene_window([[("laptop-A", BOX_A)]])
    out = make_reconciler().process(w, OmniResult(status="error", error="timeout", model="m"), [[]])
    assert out.status == "invalid" and inv.posted == []


def test_ac_per_08_post_failure_reported_not_raised(make_reconciler):
    api = FakeInventory(fail_post=True)
    out, _ = process(make_reconciler(api=api), [[("laptop-A", BOX_A)]], [obj("laptop", BOX_A)],
                     {"laptop-A": "laptop"})
    assert out.status == "error" and out.error


def test_ac_per_08_window_with_no_objects_still_posts_summary(make_reconciler, inv):
    out, _ = process(make_reconciler(), [[]], [], {})
    assert out.status == "posted"
    assert inv.last["assignments"] == []
    assert inv.last["window"]["scene_summary"]


def test_ac_per_08_timestamps_serialised_as_iso8601(make_reconciler, inv):
    process(make_reconciler(), [[("laptop-A", BOX_A)]], [obj("laptop", BOX_A)], {"laptop-A": "laptop"})
    s = inv.last["assignments"][0]["sighting"]["seen_at"]
    assert parse_dt(s) == T0 and iso(parse_dt(s))
