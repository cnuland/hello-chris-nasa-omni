"""AC-PER-29..31 revisit dedup (cluster integration 2026-10-17, docs/QUESTIONS.md #29 and #27).

The full ISS tour ran in a loop: 101 windows (6 frames, 12 s stride), 8 modules. New entities per 10 windows were
19, 22, 16, then 7, 6, 9, 5, 10, then 22 right after the video looped back to Columbus about 15 min later. Omni-only
sightings (no exemplar, AC-PER-22) attached only to an entity seen within ``no_exemplar_match_s`` (600 s).

- AC-PER-29 sightings without an exemplar: no time limit with one candidate in the module; the decision model with
  several; fallback = same-zone / most recent candidate + low_confidence review, never a new entity
- AC-PER-30 detector-linked revisits in the ask band go to the decision model with revisit context
- AC-PER-31 UNKNOWN label key: "spacesuit" == "space suit" (UNKNOWN-vs-UNKNOWN only)
"""
from __future__ import annotations

from datetime import timedelta

import pytest
from support import T0, FakeInventory, obj, parse_dt
from test_ac_per_08_reconciler import BOX_A, BOX_B, const, option_keys, process, vec

from perception import NEW
from perception.fakes import FakeDecision
from perception.labels import normalise_label

HANDHELD = "cam-handheld"  # roaming camera: the location comes from the hint, like the NASA tour footage
COLUMBUS, ZVEZDA = "Columbus", "Zvezda Service Module"
CAMERA_BOX = (0.40, 0.10, 0.50, 0.25)
SUIT_BOX = (0.60, 0.10, 0.90, 0.90)
DRILL_BOX = (0.40, 0.60, 0.50, 0.75)
LAB_ZONES = {"cam-lab1-fwd": {"LAB1S2": (0.0, 0.0, 0.5, 1.0), "LAB1P3": (0.5, 0.0, 1.0, 1.0)}}
GAP = timedelta(minutes=15)


def new_entities(payload) -> list[dict]:
    return [a["new_entity"] for a in payload["assignments"] if a.get("entity_id") is None]


def live(inv: FakeInventory, catalog_id=...) -> list[dict]:
    return [e for e in inv.live() if catalog_id is ... or e["catalog_id"] == catalog_id]


def columbus_scene(rec, t0, *, decision_labels=True):
    """Columbus: laptop-A detector-linked (exemplar), a camera and a space suit seen by omni only (no exemplar)."""
    frames = [[("laptop-A", BOX_A)]]
    objects = [obj("laptop", BOX_A, description="laptop with a red sticker"),
               obj("camera", CAMERA_BOX, description="black camera on a strap"),
               obj("space suit", SUIT_BOX, description="white space suit on the wall")]
    return process(rec, frames, objects, {"laptop-A": "laptop"}, location_hint=COLUMBUS, t0=t0)


def tour(rec, t_start, minutes: int):
    """Other modules for ``minutes`` minutes (a window per minute), ending in Zvezda 12 s before ``t_start +
    minutes``: a laptop-Z (another laptop) and a drill there."""
    for k in range(1, minutes):
        process(rec, [[("laptop-Z", BOX_B)]], [obj("laptop", BOX_B), obj("drill", DRILL_BOX)],
                {"laptop-Z": "laptop"}, location_hint=ZVEZDA, t0=t_start + timedelta(minutes=k))
    process(rec, [[("laptop-Z", BOX_B)]], [obj("laptop", BOX_B), obj("drill", DRILL_BOX)], {"laptop-Z": "laptop"},
            location_hint=ZVEZDA, t0=t_start + timedelta(minutes=minutes) - timedelta(seconds=12))


# ---------------------------------------------------------------------------------------------- the measured case
def test_ac_per_29_revisit_after_15_min_creates_no_new_entities(make_reconciler, inv):
    rec = make_reconciler(HANDHELD)
    columbus_scene(rec, T0)
    first = {e["entity_id"] for e in inv.live() if e["last_location"] == "COL1"}
    assert len(first) == 3
    tour(rec, T0, 15)
    n_before = len(inv.live())
    columbus_scene(rec, T0 + GAP)
    revisit = inv.last
    assert new_entities(revisit) == [], new_entities(revisit)
    assert {a["entity_id"] for a in revisit["assignments"]} == first
    assert len(inv.live()) == n_before


def test_ac_per_29_revisit_after_hours_still_attaches(make_reconciler, inv):
    rec = make_reconciler(HANDHELD)
    columbus_scene(rec, T0)
    columbus_scene(rec, T0 + timedelta(hours=6))
    assert new_entities(inv.last) == []
    assert len(inv.live()) == 3


def test_ac_per_29_impossible_transit_gates_only_the_zvezda_candidate(make_reconciler, inv):
    """An identical-looking laptop last seen in Zvezda 12 s ago cannot be in Columbus now (gated); the Columbus
    laptop last seen 15 min ago is still a candidate and is assigned."""
    col = inv.add_entity(catalog_id="laptop", embedding=vec("laptop-A"), last_location="COL1",
                         last_seen_at=T0 - GAP)
    zv = inv.add_entity(catalog_id="laptop", embedding=vec("laptop-A"), last_location="ZVEZDA",
                        last_seen_at=T0 - timedelta(seconds=12))
    process(make_reconciler(HANDHELD), [[("laptop-A", BOX_A)]], [obj("laptop", BOX_A)], {"laptop-A": "laptop"},
            location_hint=COLUMBUS)
    (a,) = inv.last["assignments"]
    assert a["entity_id"] == col != zv


def test_ac_per_29_truly_new_object_type_on_revisit_is_created(make_reconciler, inv):
    rec = make_reconciler(HANDHELD)
    columbus_scene(rec, T0)
    process(rec, [[]], [obj("camera", CAMERA_BOX), obj("multimeter", DRILL_BOX)], {}, location_hint=COLUMBUS,
            t0=T0 + GAP)
    news = new_entities(inv.last)
    assert [n["catalog_id"] for n in news] == ["multimeter"]


# ---------------------------------------------------------------------------------------------- one candidate
def test_ac_per_29_single_stale_candidate_in_the_module_is_attached(make_reconciler, inv):
    eid = inv.add_entity(catalog_id="laptop", last_location="LAB1", last_seen_at=T0 - timedelta(seconds=601))
    process(make_reconciler(), [[]], [obj("laptop", BOX_A)], {})
    (a,) = inv.last["assignments"]
    assert a["entity_id"] == eid
    assert inv.last["reviews"] == [] and inv.last["decisions"] == []


def test_ac_per_29_single_stale_candidate_in_a_zone_of_the_module_is_attached(make_reconciler, inv):
    eid = inv.add_entity(catalog_id="laptop", last_location="LAB1P3", last_seen_at=T0 - timedelta(hours=2))
    process(make_reconciler(), [[]], [obj("laptop", BOX_A)], {})
    assert inv.last["assignments"][0]["entity_id"] == eid


def test_ac_per_29_stale_candidate_in_another_module_is_not_attached(make_reconciler, inv):
    inv.add_entity(catalog_id="laptop", last_location="NOD2", last_seen_at=T0 - GAP)
    process(make_reconciler(), [[]], [obj("laptop", BOX_A)], {})
    assert inv.last["assignments"][0]["entity_id"] is None


def test_ac_per_29_coarse_location_keeps_the_horizon(make_reconciler, inv):
    """cam-handheld without a hint is at USOS (a segment): too coarse to attach after an arbitrary gap."""
    inv.add_entity(catalog_id="laptop", last_location="USOS", last_seen_at=T0 - GAP)
    process(make_reconciler(HANDHELD), [[]], [obj("laptop", BOX_A)], {})
    assert inv.last["assignments"][0]["entity_id"] is None


def test_ac_per_29_recent_candidate_still_wins_without_a_decision(make_reconciler, inv):
    inv.add_entity(catalog_id="laptop", last_location="LAB1", last_seen_at=T0 - GAP)
    recent = inv.add_entity(catalog_id="laptop", last_location="LAB1", last_seen_at=T0 - timedelta(seconds=30))
    dec = FakeDecision()
    process(make_reconciler(decision=dec), [[]], [obj("laptop", BOX_A)], {})
    assert inv.last["assignments"][0]["entity_id"] == recent
    assert dec.calls == []


# ---------------------------------------------------------------------------------------------- several candidates
def two_stale_laptops(inv):
    s2 = inv.add_entity(catalog_id="laptop", last_location="LAB1S2", last_seen_at=T0 - timedelta(seconds=1000))
    p3 = inv.add_entity(catalog_id="laptop", last_location="LAB1P3", last_seen_at=T0 - timedelta(seconds=900))
    inv.entities[s2]["description"] = "laptop with a red sticker"
    inv.entities[p3]["description"] = "laptop with a blue case"
    return s2, p3


def test_ac_per_29_several_candidates_ask_the_decision_model(make_reconciler, inv):
    s2, p3 = two_stale_laptops(inv)
    dec = FakeDecision(lambda q, options, state: {"choice": p3, "probabilities": {p3: 0.9, s2: 0.05, NEW: 0.05}})
    process(make_reconciler(decision=dec, camera_zones=LAB_ZONES), [[]],
            [obj("laptop", BOX_A, description="laptop with a blue case")], {})
    (a,) = inv.last["assignments"]
    assert a["entity_id"] == p3
    ((question, options, state),) = dec.calls
    assert question == "same_or_new"
    assert set(option_keys(options)) == {s2, p3, NEW}
    assert option_keys(options)[0] == s2  # the candidate last seen in the observation's zone (LAB1S2) first
    assert state["observation"]["description"] == "laptop with a blue case"
    assert state["observation"]["location"] == "LAB1S2"
    cand = state["candidates"][p3]
    assert cand["description"] == "laptop with a blue case"
    assert cand["last_location"] == "LAB1P3"
    assert parse_dt(cand["last_seen_at"]) == T0 - timedelta(seconds=900)
    assert cand["seen_ago_s"] == pytest.approx(900, abs=1)
    (logged,) = inv.last["decisions"]
    assert logged["question"] == "same_or_new" and logged["choice"] == p3
    assert "description" not in str(logged["subject"])
    assert inv.last["reviews"] == []


def test_ac_per_29_decision_down_falls_back_to_the_same_zone_candidate(make_reconciler, inv):
    s2, p3 = two_stale_laptops(inv)  # p3 is more recent, but the laptop is seen in LAB1S2
    process(make_reconciler(decision=FakeDecision(), camera_zones=LAB_ZONES), [[]], [obj("laptop", BOX_A)], {})
    (a,) = inv.last["assignments"]
    assert a["entity_id"] == s2
    assert new_entities(inv.last) == []
    (review,) = inv.last["reviews"]
    assert review["kind"] == "low_confidence"
    assert set(review["candidate_ids"]) == {s2, p3}
    assert review["image_uri"]  # a capture window: the frame is the evidence
    assert len(inv.last["decisions"]) == 1  # the failed call is logged too


def test_ac_per_29_decision_down_without_same_zone_candidate_takes_the_most_recent(make_reconciler, inv):
    inv.add_entity(catalog_id="laptop", last_location="LAB1O3", last_seen_at=T0 - timedelta(seconds=1000))
    recent = inv.add_entity(catalog_id="laptop", last_location="LAB1P3", last_seen_at=T0 - timedelta(seconds=900))
    process(make_reconciler(decision=FakeDecision(), camera_zones={"cam-lab1-fwd": {"LAB1S2": (0, 0, 0.5, 1)}}),
            [[]], [obj("laptop", BOX_A)], {})
    assert inv.last["assignments"][0]["entity_id"] == recent
    assert [r["kind"] for r in inv.last["reviews"]] == ["low_confidence"]


@pytest.mark.parametrize("script", [
    {"choice": "not-a-candidate", "probabilities": {"not-a-candidate": 0.99}},
    {"choice": "PLACEHOLDER", "probabilities": {"PLACEHOLDER": 0.4}},  # below decision_accept
    {"choice": "PLACEHOLDER", "probabilities": {"PLACEHOLDER": 0.95}, "needs_review": True},
])
def test_ac_per_29_unusable_decisions_fall_back(make_reconciler, inv, script):
    s2, p3 = two_stale_laptops(inv)
    script = {k: ({p3 if kk == "PLACEHOLDER" else kk: vv for kk, vv in v.items()} if isinstance(v, dict) else
                  (p3 if v == "PLACEHOLDER" else v)) for k, v in script.items()}
    process(make_reconciler(decision=FakeDecision([script]), camera_zones=LAB_ZONES), [[]], [obj("laptop", BOX_A)],
            {})
    assert inv.last["assignments"][0]["entity_id"] == s2
    assert [r["kind"] for r in inv.last["reviews"]] == ["low_confidence"]


def test_ac_per_29_confident_new_creates_an_entity(make_reconciler, inv):
    two_stale_laptops(inv)
    dec = FakeDecision([{"choice": NEW, "probabilities": {NEW: 0.9}}])
    process(make_reconciler(decision=dec, camera_zones=LAB_ZONES), [[]], [obj("laptop", BOX_A)], {})
    assert [n["catalog_id"] for n in new_entities(inv.last)] == ["laptop"]
    assert inv.last["reviews"] == []


def test_ac_per_29_confident_new_with_expected_count_reached_prefers_attaching(make_reconciler, inv):
    """camera-dslr expects 4: with 4 live cameras a 5th is a certain duplicate."""
    ids = [inv.add_entity(catalog_id="camera-dslr", last_location=loc, last_seen_at=T0 - timedelta(seconds=s))
           for loc, s in (("LAB1S2", 2000), ("LAB1P3", 1900), ("LAB1", 1800), ("NOD2", 1700))]
    dec = FakeDecision([{"choice": NEW, "probabilities": {NEW: 0.9}}])
    process(make_reconciler(decision=dec, camera_zones=LAB_ZONES), [[]], [obj("camera", BOX_A)], {})
    (a,) = inv.last["assignments"]
    assert a["entity_id"] == ids[0]  # the same-zone candidate
    assert "low_confidence" in [r["kind"] for r in inv.last["reviews"]]
    assert len(live(inv, "camera-dslr")) == 4


def test_ac_per_29_two_omni_only_laptops_in_two_zones_keep_their_own_entities(make_reconciler, inv):
    s2, p3 = two_stale_laptops(inv)
    process(make_reconciler(decision=FakeDecision(), camera_zones=LAB_ZONES), [[]],
            [obj("laptop", BOX_A), obj("laptop", BOX_B)], {})
    by_zone = {a["sighting"]["location_id"]: a["entity_id"] for a in inv.last["assignments"]}
    assert by_zone == {"LAB1S2": s2, "LAB1P3": p3}


def test_ac_per_29_private_window_attaches_without_reviews_or_uploads(make_reconciler, inv, storage):
    a1 = inv.add_entity(catalog_id="laptop", last_location="NOD2", last_seen_at=T0 - timedelta(seconds=2000))
    inv.add_entity(catalog_id="laptop", last_location="NOD2", last_seen_at=T0 - timedelta(seconds=1000))
    inv.entities[a1]["description"] = "laptop with crew name tag"
    dec = FakeDecision()
    process(make_reconciler("cam-nod2-aft", decision=dec), [[]],
            [obj("laptop", BOX_A, description="laptop on the sleeping bag")], {},
            location_hint="Node 2 crew quarters (stbd)")
    payload = inv.last
    assert new_entities(payload) == []
    assert payload["reviews"] == []
    assert storage.objects == {}
    assert "sleeping bag" not in str(payload["decisions"]) and "name tag" not in str(payload["decisions"])


# ---------------------------------------------------------------------------------------------- detector-linked
def test_ac_per_30_detector_linked_revisit_in_ask_band_asks_with_revisit_context(make_reconciler, inv):
    eid = inv.add_entity(catalog_id="laptop", embedding=vec("laptop-A"), last_location="LAB1S2",
                         last_seen_at=T0 - GAP)
    inv.entities[eid]["description"] = "laptop with a red sticker"
    dec = FakeDecision(lambda q, options, state: {"choice": eid, "probabilities": {eid: 0.9, NEW: 0.1}})
    process(make_reconciler(decision=dec, scorer=const(0.7)), [[("laptop-A", BOX_A)]], [obj("laptop", BOX_A)],
            {"laptop-A": "laptop"})
    assert inv.last["assignments"][0]["entity_id"] == eid
    ((_, _, state),) = dec.calls
    cand = state["candidates"][eid]
    assert cand["last_location"] == "LAB1S2"
    assert cand["seen_ago_s"] == pytest.approx(900, abs=1)
    assert cand["description"] == "laptop with a red sticker"


def test_ac_per_30_different_laptop_in_another_zone_stays_separate(make_reconciler, inv):
    """The detector and the appearance distinguish them: laptop-B is not merged into laptop-A's entity, also after a
    long gap (adoption excludes entities compared by appearance)."""
    a = inv.add_entity(catalog_id="laptop", embedding=vec("laptop-A"), last_location="LAB1S2",
                       last_seen_at=T0 - GAP)
    process(make_reconciler(camera_zones=LAB_ZONES), [[("laptop-B", BOX_B)]], [obj("laptop", BOX_B)],
            {"laptop-B": "laptop"})
    (asg,) = inv.last["assignments"]
    assert asg["entity_id"] is None and asg["sighting"]["location_id"] == "LAB1P3"
    assert len(live(inv, "laptop")) == 2 and a in inv.entities


# ---------------------------------------------------------------------------------------------- UNKNOWN label key
def test_ac_per_31_label_key_ignores_spaces_and_hyphens():
    from perception.labels import label_key

    assert label_key("spacesuit") == label_key("space suit") == label_key("Space-Suit") == "spacesuit"
    assert label_key("  ") == ""
    assert normalise_label("Space-Suit") == "space suit"  # display behaviour unchanged


def test_ac_per_31_space_suit_attaches_to_spacesuit(make_reconciler, inv):
    eid = inv.add_entity(catalog_id=None, label="spacesuit", last_location="LAB1", status="unknown",
                         last_seen_at=T0 - timedelta(seconds=60))
    process(make_reconciler(), [[]], [obj("space suit", SUIT_BOX)], {})
    (a,) = inv.last["assignments"]
    assert a["entity_id"] == eid
    assert inv.last["reviews"] == []


def test_ac_per_31_unknown_variants_consolidate_within_a_window(make_reconciler, inv):
    process(make_reconciler(), [[], []], [obj("spacesuit", SUIT_BOX, frame_index=0),
                                         obj("space-suit", SUIT_BOX, frame_index=1)], {})
    assert len(inv.last["assignments"]) == 1


def test_ac_per_31_different_unknown_labels_stay_separate(make_reconciler, inv):
    inv.add_entity(catalog_id=None, label="space suit", last_location="LAB1", status="unknown",
                   last_seen_at=T0 - timedelta(seconds=60))
    process(make_reconciler(), [[]], [obj("space helmet", SUIT_BOX)], {})
    assert inv.last["assignments"][0]["entity_id"] is None


def test_ac_per_31_omni_event_targets_the_unknown_by_key(make_reconciler, inv):
    events = [{"type": "used", "object_label": "space suit", "actor_ref": None, "to_location_hint": None,
               "frame_index": 0, "evidence": "suit handled", "confidence": 0.9}]
    process(make_reconciler(), [[]], [obj("spacesuit", SUIT_BOX)], {}, events=events)
    assert [e["event_type"] for e in inv.last["events"]] == ["used"]


# ---------------------------------------------------------------------------------------------- performance
def test_ac_per_29_one_items_query_per_class_and_module_per_window(make_reconciler, inv):
    two_stale_laptops(inv)
    seen: list[dict] = []
    original = inv.list_items

    def spy(**filters):
        seen.append(filters)
        return original(**filters)

    inv.list_items = spy
    boxes = [(0.05, 0.1, 0.15, 0.2), (0.55, 0.1, 0.65, 0.2), (0.05, 0.6, 0.15, 0.7)]
    process(make_reconciler(camera_zones=LAB_ZONES), [[]], [obj("laptop", b) for b in boxes], {})
    laptop_queries = [f for f in seen if f.get("catalog_id") == "laptop"]
    assert len(laptop_queries) == 1, laptop_queries
    assert laptop_queries[0].get("location") == "LAB1"


# ---------------------------------------------------------------------------------------------- review round
def test_ac_per_29_review_recent_candidates_prefer_the_observation_zone(make_reconciler, inv):
    """Window 2 of a revisit: both laptops were attached a moment ago (both recent). A laptop seen in LAB1S2 is
    the one last seen in LAB1S2, not the one last seen slightly later in LAB1P3."""
    s2 = inv.add_entity(catalog_id="laptop", last_location="LAB1S2", last_seen_at=T0 - timedelta(seconds=13))
    inv.add_entity(catalog_id="laptop", last_location="LAB1P3", last_seen_at=T0 - timedelta(seconds=12))
    process(make_reconciler(camera_zones=LAB_ZONES), [[]], [obj("laptop", BOX_A)], {})
    assert inv.last["assignments"][0]["entity_id"] == s2


def test_ac_per_29_review_twins_revisited_omni_only_keep_their_zones_over_two_windows(make_reconciler, inv):
    s2, p3 = two_stale_laptops(inv)
    rec = make_reconciler(decision=FakeDecision(), camera_zones=LAB_ZONES)
    for k in range(2):
        process(rec, [[]], [obj("laptop", BOX_A), obj("laptop", BOX_B)], {}, t0=T0 + timedelta(seconds=12 * k))
        by_zone = {a["sighting"]["location_id"]: a["entity_id"] for a in inv.last["assignments"]}
        assert by_zone == {"LAB1S2": s2, "LAB1P3": p3}, k
    # window 3: only the LAB1P3 laptop is in view; both entities are recent
    process(rec, [[]], [obj("laptop", BOX_B)], {}, t0=T0 + timedelta(seconds=36))
    assert inv.last["assignments"][0]["entity_id"] == p3


def test_ac_per_29_review_inventory_failure_leaves_the_sighting_new(make_reconciler, inv):
    def broken(**filters):
        raise RuntimeError("inventory-api down")

    inv.add_entity(catalog_id="laptop", last_location="LAB1", last_seen_at=T0 - GAP)
    inv.list_items = broken
    outcome, _ = process(make_reconciler(), [[]], [obj("laptop", BOX_A)], {})
    assert outcome.status == "posted"
    assert inv.last["assignments"][0]["entity_id"] is None


def test_ac_per_29_review_raising_decision_model_falls_back(make_reconciler, inv):
    s2, _ = two_stale_laptops(inv)

    def boom(question, options, state):
        raise TimeoutError("decision model timed out")

    process(make_reconciler(decision=FakeDecision(boom), camera_zones=LAB_ZONES), [[]], [obj("laptop", BOX_A)], {})
    assert inv.last["assignments"][0]["entity_id"] == s2
    assert [r["kind"] for r in inv.last["reviews"]] == ["low_confidence"]


def test_ac_per_29_review_capture_window_review_names_no_private_candidate_details(make_reconciler, inv):
    """A Node 2 (capture) sighting whose candidates were last seen in crew quarters: the review carries the Node 2
    frame and the observation's own words, never the private candidates' descriptions."""
    a1 = inv.add_entity(catalog_id="laptop", last_location="NOD2S5", last_seen_at=T0 - timedelta(seconds=2000))
    a2 = inv.add_entity(catalog_id="laptop", last_location="NOD2D5", last_seen_at=T0 - timedelta(seconds=1000))
    inv.entities[a1]["description"] = "laptop with crew name tag"
    inv.entities[a2]["description"] = "laptop on the sleeping bag"
    process(make_reconciler("cam-nod2-aft"), [[]], [obj("laptop", BOX_A, description="grey laptop")], {})
    (review,) = inv.last["reviews"]
    assert review["kind"] == "low_confidence" and review["location_id"] == "NOD2"
    assert "name tag" not in str(inv.last) and "sleeping bag" not in str(inv.last)
