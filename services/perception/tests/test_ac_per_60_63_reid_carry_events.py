"""UI review round 5 (docs/ui-review/round5/findings.json):

AC-PER-60 (UI5-03) re-ID teleport: laptop 8535788a went JPM1 (0.90) -> USOS -> ZVEZDA (0.50) with no move event. The
decision log shows the Zvezda match was the decision model choosing it at 0.83 with appearance similarity 0.61 (p 0.63,
ask band). For a generic class a match across modules needs the auto band; a module change emits a moved event.

AC-PER-61 (UI5-04) the module carry labelled Node 1 frames (window 063138, location_hint "NOD 3" from a "To NOD 3"
sign) as CUP1: an unresolved caption-like hint ends it.

AC-PER-63 (UI5-09) a first sighting emitted "Moved by an unknown person": no moved event without a from_location, and
a person's own transit is never a "moved" event (an arrival is a "seen" event at the new place)."""
from __future__ import annotations

from datetime import timedelta

import pytest
from support import T0, blend, detections_for, obj, observation, random_unit, scene_window
from test_ac_per_08_reconciler import BOX_A, vec

from perception import NEW
from perception.config import PerceptionConfig
from perception.fakes import FakeDecision
from perception.types import DecisionResult, OmniResult

HANDHELD = "cam-handheld"


def process(rec, objects, *, hint=None, summary="crew working in the module", t=0.0, people=(), events=(),
            frames=None, labels=None):
    frames = frames if frames is not None else [[("laptop-A", BOX_A)]]
    w = scene_window(frames, t0=T0 + timedelta(seconds=t))
    omni = OmniResult(status="ok", observation=observation(objects, people=people, events=events,
                                                           location_hint=hint, scene_summary=summary),
                      model="fake-omni", raw="{}")
    out = rec.process(w, omni, detections_for(frames, labels if labels is not None else {"laptop-A": "laptop"}))
    assert out.status == "posted", out.error
    return out.payload


def const(p):
    return lambda features: p


def accept(eid, p=0.83):
    return FakeDecision(lambda q, o, s: DecisionResult(choice=eid, probabilities={eid: p, NEW: 1 - p},
                                                       model="fake-decision"))


def moved(payload):
    return [e for e in payload["events"] if e["event_type"] == "moved"]


# ================================================================================ AC-PER-60 re-ID across modules
def test_live_case_generic_laptop_in_another_module_is_not_absorbed_in_the_ask_band(make_reconciler, inv):
    """The Zvezda sighting: candidate last seen in JPM1, p 0.63 (ask band), the decision model would say 'same' at
    0.83. Now: no decision call, a new provisional laptop and a possible_duplicate review naming the JPM1 laptop."""
    eid = inv.add_entity(catalog_id="laptop", embedding=blend(vec("laptop-A"), random_unit(3), 0.7),
                         last_location="JPM1", last_seen_at=T0 - timedelta(minutes=6))
    dec = accept(eid)
    p = process(make_reconciler(HANDHELD, decision=dec, scorer=const(0.63)), [obj("laptop", BOX_A)], hint="Zvezda")
    [a] = p["assignments"]
    assert a["entity_id"] is None and a["new_entity"]["status"] == "provisional"
    assert a["sighting"]["location_id"] == "ZVEZDA"
    dup = [r for r in p["reviews"] if r["kind"] == "possible_duplicate"]
    assert len(dup) == 1 and eid in [str(c) for c in dup[0]["candidate_ids"]]
    assert dec.calls == []
    assert moved(p) == []


def test_generic_laptop_in_another_module_matches_in_the_auto_band_and_emits_a_move(make_reconciler, inv):
    eid = inv.add_entity(catalog_id="laptop", embedding=vec("laptop-A"), last_location="JPM1",
                         last_seen_at=T0 - timedelta(minutes=6))
    p = process(make_reconciler(HANDHELD, scorer=const(0.9)), [obj("laptop", BOX_A)], hint="Zvezda")
    [a] = p["assignments"]
    assert str(a["entity_id"]) == eid
    [ev] = moved(p)
    assert ev["entity_ref"] == a["ref"] and ev["from_location"] == "JPM1" and ev["to_location"] == "ZVEZDA"
    assert ev["actor_desc"] is None and ev["source"] == "pipeline"
    assert ev["confidence"] == pytest.approx(a["sighting"]["confidence"])


def test_generic_laptop_in_the_same_module_still_asks_the_decision_model(make_reconciler, inv):
    eid = inv.add_entity(catalog_id="laptop", embedding=blend(vec("laptop-A"), random_unit(3), 0.7),
                         last_location="JPM1", last_seen_at=T0 - timedelta(minutes=6))
    dec = accept(eid)
    p = process(make_reconciler(HANDHELD, decision=dec, scorer=const(0.63)), [obj("laptop", BOX_A)], hint="Kibo")
    assert str(p["assignments"][0]["entity_id"]) == eid
    assert len(dec.calls) == 1 and moved(p) == []


def test_same_module_candidate_is_still_offered_when_a_cross_module_one_is_held_back(make_reconciler, inv):
    far = inv.add_entity(catalog_id="laptop", embedding=vec("laptop-A"), last_location="JPM1",
                         last_seen_at=T0 - timedelta(minutes=6))
    near = inv.add_entity(catalog_id="laptop", embedding=blend(vec("laptop-A"), random_unit(5), 0.6),
                          last_location="ZVEZDA", last_seen_at=T0 - timedelta(minutes=20))
    seen = []

    def answer(q, options, state):
        seen.append(list(options))
        return DecisionResult(choice=near, probabilities={near: 0.9, NEW: 0.1}, model="fake-decision")

    p = process(make_reconciler(HANDHELD, decision=FakeDecision(answer), scorer=const(0.7)), [obj("laptop", BOX_A)],
                hint="Zvezda")
    assert seen and far not in seen[0] and near in seen[0]
    assert str(p["assignments"][0]["entity_id"]) == near


def test_non_generic_class_keeps_the_decision_model_across_modules_and_emits_a_move(make_reconciler, inv):
    eid = inv.add_entity(catalog_id="drill-pistol", embedding=blend(vec("drill-A"), random_unit(3), 0.7),
                         last_location="JPM1", last_seen_at=T0 - timedelta(minutes=6))
    p = process(make_reconciler(HANDHELD, decision=accept(eid), scorer=const(0.63)), [obj("drill", BOX_A)],
                hint="Zvezda", frames=[[("drill-A", BOX_A)]], labels={"drill-A": "drill"})
    assert str(p["assignments"][0]["entity_id"]) == eid
    [ev] = moved(p)
    assert (ev["from_location"], ev["to_location"]) == ("JPM1", "ZVEZDA")


def test_segment_level_sighting_is_not_a_module_change(make_reconciler, inv):
    eid = inv.add_entity(catalog_id="laptop", embedding=vec("laptop-A"), last_location="JPM1",
                         last_seen_at=T0 - timedelta(minutes=6))
    p = process(make_reconciler(HANDHELD, scorer=const(0.9)), [obj("laptop", BOX_A)])  # USOS, no module
    assert str(p["assignments"][0]["entity_id"]) == eid and moved(p) == []


def test_omni_move_for_the_same_entity_is_not_duplicated(make_reconciler, inv):
    eid = inv.add_entity(catalog_id="laptop", embedding=vec("laptop-A"), last_location="JPM1",
                         last_seen_at=T0 - timedelta(minutes=6))
    ev = {"type": "moved", "object_label": "laptop", "actor_ref": "P1", "to_location_hint": "Zvezda",
          "frame_index": 0, "confidence": 0.8, "evidence": "carried in"}
    people = [{"ref": "P1", "appearance": "blue shirt", "activity": "carrying", "is_speaking": False}]
    p = process(make_reconciler(HANDHELD, scorer=const(0.9)), [obj("laptop", BOX_A)], hint="Zvezda",
                events=[ev], people=people)
    [m] = moved(p)
    assert m["actor_desc"] == "blue shirt" and str(p["assignments"][0]["entity_id"]) == eid


def test_person_tracks_never_get_a_synthesised_move(make_reconciler, inv):
    eid = inv.add_entity(catalog_id="crew-member", kind="person", last_location="JPM1",
                         last_seen_at=T0 - timedelta(seconds=120), description="blue shirt, khaki pants")
    people = [{"ref": "P1", "appearance": "blue shirt, khaki pants", "activity": "floating", "is_speaking": False}]
    p = process(make_reconciler(HANDHELD), [], hint="Zvezda", people=people, frames=[[]], labels={})
    assert [str(a["entity_id"]) for a in p["assignments"]] == [eid]
    assert moved(p) == []


def test_generic_classes_config():
    cfg = PerceptionConfig()
    assert {"laptop", "tablet", "camera", "cargo transfer bag", "m-bag"} <= set(cfg.generic_classes)
    cfg = PerceptionConfig.from_env({"PERCEPTION_GENERIC_CLASSES": "laptop, Drill "})
    assert cfg.generic_classes == ("laptop", "drill")
    assert PerceptionConfig.from_env({"PERCEPTION_GENERIC_CLASSES": "none"}).generic_classes == ()


def test_generic_class_by_catalog_id_or_class_name(make_reconciler, inv):
    eid = inv.add_entity(catalog_id="drill-pistol", embedding=blend(vec("drill-A"), random_unit(3), 0.7),
                         last_location="JPM1", last_seen_at=T0 - timedelta(minutes=6))
    dec = accept(eid)
    rec = make_reconciler(HANDHELD, decision=dec, scorer=const(0.63), generic_classes=("drill-pistol",))
    p = process(rec, [obj("drill", BOX_A)], hint="Zvezda", frames=[[("drill-A", BOX_A)]], labels={"drill-A": "drill"})
    assert p["assignments"][0]["entity_id"] is None and dec.calls == []


# ================================================================================ AC-PER-61 carry ends on scene change
def _carry_run(rec, start, hint=None, summary="crew working in the module"):
    p = process(rec, [obj("laptop", BOX_A)], hint=hint, summary=summary, t=start)
    return p["window"]["location"], p["assignments"][0]["sighting"]["location_id"]


def test_live_case_unresolved_caption_hint_ends_the_carry(make_reconciler):
    """063108 (hint 'Cupola') then 063138 (hint 'NOD 3', a 'To NOD 3' sign in Node 1), 30 s later."""
    rec = make_reconciler(HANDHELD, revisit_location_sim=0.0)  # AC-PER-67 off: the synthetic frames are identical
    assert _carry_run(rec, 0, hint="Cupola")[1] == "CUP1"
    loc, sighting = _carry_run(rec, 30, hint="NOD 3",
                               summary="A person in a grey shirt works near a rack while another person is visible "
                                       "in the background.")
    assert sighting == "USOS" and loc["source"] == "camera"
    assert loc.get("carry_ended") == "caption"


def test_carry_stays_ended_after_a_scene_change(make_reconciler):
    rec = make_reconciler(HANDHELD, revisit_location_sim=0.0)  # AC-PER-67 off: the synthetic frames are identical
    _carry_run(rec, 0, hint="Cupola")
    _carry_run(rec, 10, hint="To COL")
    assert _carry_run(rec, 20)[1] == "USOS"


def test_view_through_a_hatch_into_another_module_keeps_the_carry(make_reconciler):
    """Real window 03:04:13 (truth JPM1): from inside Kibo, 'A view through a hatch into another module ...'. A summary
    transit rule was tried and dropped: on the 51 labelled windows it lost this correct carry and gained nothing."""
    rec = make_reconciler(HANDHELD)
    _carry_run(rec, 0, hint="Kibo")
    loc, sighting = _carry_run(rec, 30, summary="A view through a hatch into another module of the ISS shows a large "
                                                "white covered object in the foreground.")
    assert sighting == "JPM1" and loc["source"] == "carried"


def test_summary_naming_another_module_still_ends_the_carry(make_reconciler):
    rec = make_reconciler(HANDHELD)
    _carry_run(rec, 0, hint="Kibo")
    loc, sighting = _carry_run(rec, 20, summary="An astronaut floats toward Harmony.")
    assert sighting == "USOS" and loc["source"] != "carried"


@pytest.mark.parametrize("hint,summary", [
    (None, "Crew working near a rack."),
    ("", "A person floats near a hatch."),  # a hatch in view is not a transit
    ("US Orbital Segment", "crew working"),  # a segment hint containing the anchor keeps the carry (AC-PER-41)
])
def test_no_scene_change_keeps_the_carry(make_reconciler, hint, summary):
    rec = make_reconciler(HANDHELD)
    _carry_run(rec, 0, hint="Kibo")
    loc, sighting = _carry_run(rec, 20, hint=hint, summary=summary)
    assert sighting == "JPM1" and loc["source"] == "carried"


def test_live_cupola_person_colour_clash_holds():
    """The CUP1 sighting 395bae46 was not an attach: Gemma described the person (blue shirt and grey trousers in the
    frame) as 'grey long-sleeve shirt, blue shorts' and the track was created from it. The clash rule itself holds."""
    from perception.people import appearance_conflict

    assert appearance_conflict("blue shirt", "grey long-sleeve shirt, blue shorts")
    assert appearance_conflict("blue shirt, orange helmet", "grey long-sleeve shirt, blue shorts")
    assert appearance_conflict("grey long-sleeve shirt, blue shorts", "grey shirt, black shorts, headset")


# ================================================================================ AC-PER-63 moved events
MOVE = {"type": "moved", "object_label": "camera", "actor_ref": None, "to_location_hint": None, "frame_index": 0,
        "confidence": 0.8, "evidence": "camera on the arm"}


def test_live_case_first_sighting_emits_no_moved_event(make_reconciler):
    """The camera on the robotic arm: from_location null -> USOS was shown as 'Moved by an unknown person'."""
    p = process(make_reconciler(HANDHELD), [obj("camera", BOX_A)], events=[MOVE],
                frames=[[("cam-A", BOX_A)]], labels={"cam-A": "camera"})
    assert p["assignments"][0]["new_entity"] is not None
    assert moved(p) == [] and p["events"] == []


def test_object_move_with_a_known_previous_place_is_kept(make_reconciler, inv):
    eid = inv.add_entity(catalog_id="camera-dslr", embedding=vec("cam-A"), last_location="LAB1",
                         last_seen_at=T0 - timedelta(minutes=30))
    p = process(make_reconciler(HANDHELD, scorer=const(0.95)), [obj("camera", BOX_A)], hint="Kibo",
                events=[dict(MOVE, object_label="camera")], frames=[[("cam-A", BOX_A)]], labels={"cam-A": "camera"})
    assert str(p["assignments"][0]["entity_id"]) == eid
    [m] = moved(p)
    assert (m["from_location"], m["to_location"]) == ("LAB1", "JPM1")


def test_person_transit_is_an_arrival_seen_event_never_moved(make_reconciler, inv):
    eid = inv.add_entity(catalog_id="crew-member", kind="person", last_location="JPM1",
                         last_seen_at=T0 - timedelta(seconds=120), description="blue shirt, khaki pants")
    people = [{"ref": "P1", "appearance": "blue shirt, khaki pants", "activity": "floating", "is_speaking": False}]
    ev = {"type": "moved", "object_label": "person", "actor_ref": "P1", "to_location_hint": None, "frame_index": 0,
          "confidence": 0.7, "evidence": "floats in"}
    p = process(make_reconciler(HANDHELD), [], hint="Zvezda", people=people, events=[ev], frames=[[]], labels={})
    assert [str(a["entity_id"]) for a in p["assignments"]] == [eid]
    assert moved(p) == []
    [seen] = p["events"]
    assert seen["event_type"] == "seen" and seen["to_location"] == "ZVEZDA" and seen["actor_desc"] is None


def test_new_person_track_move_is_a_seen_event(make_reconciler):
    people = [{"ref": "P1", "appearance": "blue shirt", "activity": "floating", "is_speaking": False}]
    ev = {"type": "moved", "object_label": "person", "actor_ref": "P1", "to_location_hint": None, "frame_index": 0,
          "confidence": 0.7, "evidence": "floats in"}
    p = process(make_reconciler(HANDHELD), [], hint="Cupola", people=people, events=[ev], frames=[[]], labels={})
    assert [e["event_type"] for e in p["events"]] == ["seen"]
    assert p["events"][0]["to_location"] == "CUP1" and p["events"][0]["from_location"] is None
