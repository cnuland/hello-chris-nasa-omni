"""AC-PER-68 (UI6-06) catalog labels that contradict their description; AC-PER-69 (UI6-07) worn items are not
unknowns; AC-PER-70 (UI6-08) no move inside one place and no move without a destination.

Live strings (docs/ui-review/round6, raw replies read-only): drill 1bfe7717 "a black handheld vacuum cleaner being
used by a person"; "orange tool" fuzzy-mapped to the operator class "Orange ball"; wrench "orange handled tool";
wristwatch "a dark watch with a light face" and "black watch with yellow accents"; "yellow helmet" / "yellow
protective helmet" with people "blue suit, orange gloves"; laptop 8b4c0819 "moved from LAB1" (to null)."""
from __future__ import annotations

from datetime import timedelta

import pytest
from support import CATALOG, T0, detections_for, obj, observation, scene_window
from test_ac_per_08_reconciler import BOX_A, vec

from perception.config import ConfigError, PerceptionConfig
from perception.labels import CatalogMapper, is_worn_item, kind_family
from perception.types import OmniResult

HANDHELD = "cam-handheld"
ORANGE_BALL = {"catalog_id": "orange-ball", "class_name": "orange ball", "display_name": "Orange ball",
               "synonyms": [], "category": "object", "is_fungible": False, "expected_count": None,
               "home_location": None}


def process(rec, objects, *, hint="Columbus", t=0.0, frames=None, labels=None, people=(), events=(), transcript=""):
    frames = frames if frames is not None else [[]]
    w = scene_window(frames, t0=T0 + timedelta(seconds=t))
    omni = OmniResult(status="ok", observation=observation(objects, people=people, events=events, location_hint=hint,
                                                           transcript=transcript), model="fake-omni", raw="{}")
    out = rec.process(w, omni, detections_for(frames, labels or {}))
    assert out.status == "posted", out.error
    return out.payload


def person(ref, appearance, activity="floating"):
    return {"ref": ref, "appearance": appearance, "activity": activity, "is_speaking": False}


# ================================================================================ AC-PER-68 kind families + mapping
@pytest.mark.parametrize(("text", "family"), [
    ("a black handheld vacuum cleaner", "vacuum"), ("orange handled tool", "tool"), ("drill", "tool"),
    ("orange ball", "sphere"), ("pistol grip tool", "tool"), ("grey laptop", "computer"), ("iPad", "computer"),
    ("white storage bag", "bag"), ("mysterious widget", None), ("", None)])
def test_kind_family(text, family):
    assert kind_family(text) == family


def test_live_case_orange_tool_is_not_the_orange_ball_class():
    mapper = CatalogMapper([*CATALOG, ORANGE_BALL])
    assert mapper.map("orange tool").catalog_id == "UNKNOWN"
    assert mapper.map("orange ball").catalog_id == "orange-ball"  # exact names always map
    assert mapper.map("orange balls").catalog_id == "orange-ball"  # fuzzy within the family still maps


def test_mapping_guard_never_touches_exact_or_synonym_matches():
    mapper = CatalogMapper(CATALOG)
    assert mapper.map("PGT").catalog_id == "drill-pistol" and mapper.map("torch").catalog_id == "flashlight"


# ================================================================================ AC-PER-68 description check
def test_live_case_drill_described_as_a_vacuum_cleaner_is_doubted(make_reconciler, inv):
    p = process(make_reconciler(HANDHELD), [obj("drill", BOX_A, description="a black handheld vacuum cleaner being "
                                                                           "used by a person", held_by="P1")],
                frames=[[("drill-A", BOX_A)]], labels={"drill-A": "drill"},
                people=[person("P1", "grey t-shirt, dark shorts", "using a handheld vacuum cleaner")])
    [a] = [a for a in p["assignments"] if a["new_entity"]["kind"] == "object"]
    assert a["new_entity"]["catalog_id"] == "drill-pistol"  # the label is kept, but doubted
    assert a["sighting"]["confidence"] <= 0.4
    [r] = [r for r in p["reviews"] if r["kind"] == "low_confidence"]
    assert "vacuum cleaner" in r["description"] and "disagree" in r["description"]
    assert r["image_uri"]


def test_description_naming_another_catalog_class_remaps(make_reconciler, inv):
    p = process(make_reconciler(HANDHELD), [obj("wrench", BOX_A, description="a pair of scissors on the rack")])
    [a] = p["assignments"]
    assert a["new_entity"]["catalog_id"] == "scissors" and p["reviews"] == []


def test_same_family_is_no_conflict(make_reconciler, inv):
    p = process(make_reconciler(HANDHELD), [obj("wrench", BOX_A, description="orange handled tool")])
    [a] = p["assignments"]
    assert a["new_entity"]["catalog_id"] == "wrench" and p["reviews"] == [] and a["sighting"]["confidence"] == 0.9
    p = process(make_reconciler(HANDHELD), [obj("drill", BOX_A, description="a power tool with a battery")], t=30)
    assert p["reviews"] == []


def test_unknown_head_noun_is_no_conflict(make_reconciler, inv):
    p = process(make_reconciler(HANDHELD), [obj("laptop", BOX_A, description="grey device with a screen")])
    assert p["reviews"] == [] and p["assignments"][0]["new_entity"]["catalog_id"] == "laptop"


def test_never_remapped_to_the_person_class_or_a_fungible_class(make_reconciler, inv):
    p = process(make_reconciler(HANDHELD), [obj("laptop", BOX_A, description="astronaut next to a desk")])
    assert p["assignments"][0]["new_entity"]["catalog_id"] == "laptop"
    p = process(make_reconciler(HANDHELD), [obj("laptop", BOX_A, description="towel on a laptop")], hint="Kibo")
    assert p["assignments"][0]["new_entity"]["catalog_id"] == "laptop"


def test_existing_entity_sighting_is_doubted_too(make_reconciler, inv):
    eid = inv.add_entity(catalog_id="drill-pistol", embedding=vec("drill-A"), last_location="COL1",
                         last_seen_at=T0 - timedelta(minutes=1))
    p = process(make_reconciler(HANDHELD, scorer=lambda f: 0.95),
                [obj("drill", BOX_A, description="black handheld vacuum cleaner")],
                frames=[[("drill-A", BOX_A)]], labels={"drill-A": "drill"})
    [a] = p["assignments"]
    assert str(a["entity_id"]) == eid and a["sighting"]["confidence"] <= 0.4
    assert [r["kind"] for r in p["reviews"]] == ["low_confidence"]


def test_label_conflict_config():
    assert PerceptionConfig().label_conflict_confidence == 0.4
    assert PerceptionConfig.from_env({"PERCEPTION_LABEL_CONFLICT_CONFIDENCE": "0.3"}).label_conflict_confidence == 0.3
    with pytest.raises(ConfigError):
        PerceptionConfig.from_env({"PERCEPTION_LABEL_CONFLICT_CONFIDENCE": "1.2"})


# ================================================================================ AC-PER-69 worn items
@pytest.mark.parametrize(("label", "description", "people", "worn"), [
    ("wristwatch", "a dark watch with a light face", False, True),
    ("wristwatch", "black watch with yellow accents", False, True),
    ("watch", "second black watch", False, True),
    ("yellow helmet", "yellow protective helmet", True, True),
    ("yellow helmet", "yellow protective helmet", False, False),
    ("orange gloves", "pair of gloves strapped to a rail", False, False),
    ("blue cap", "worn by the crew member", False, True),
    ("bracelet", "on her wrist", False, True),
    ("goggles", "not worn, floating near the rack", False, False),
    ("orange sphere", "a small orange ball", True, False),
])
def test_is_worn_item(label, description, people, worn):
    assert is_worn_item(label, description, people) is worn


def test_live_case_wristwatch_is_not_raised(make_reconciler, inv):
    p = process(make_reconciler(HANDHELD), [obj("wristwatch", BOX_A, description="a dark watch with a light face")],
                people=[person("P1", "black shirt, grey shorts")])
    assert [a["new_entity"]["kind"] for a in p["assignments"]] == ["person"]
    assert p["reviews"] == [] and p["window"]["worn_filtered"] == 1


def test_live_case_helmet_on_a_cosmonaut_is_not_raised(make_reconciler, inv):
    p = process(make_reconciler(HANDHELD), [obj("yellow helmet", BOX_A, description="yellow protective helmet")],
                people=[person("P1", "blue suit, orange gloves")])
    assert p["window"]["worn_filtered"] == 1 and p["assignments"][0]["new_entity"]["kind"] == "person"


def test_stowed_helmet_without_people_is_still_an_unknown(make_reconciler, inv):
    p = process(make_reconciler(HANDHELD), [obj("yellow helmet", BOX_A, description="yellow protective helmet")])
    assert [r["kind"] for r in p["reviews"]] == ["unknown_object"]


def test_catalog_headset_worn_by_a_person_is_kept(make_reconciler, inv):
    p = process(make_reconciler(HANDHELD), [obj("headset", BOX_A, description="headset worn by the crew member")],
                people=[person("P1", "grey shirt, black shorts, headset")])
    assert any((a.get("new_entity") or {}).get("catalog_id") == "headset" for a in p["assignments"])
    assert p["window"]["worn_filtered"] == 0


# ================================================================================ AC-PER-70 moves
def moved(p):
    return [e for e in p["events"] if e["event_type"] == "moved"]


def _laptop(inv, location):
    return inv.add_entity(catalog_id="laptop", embedding=vec("laptop-A"), last_location=location,
                          last_seen_at=T0 - timedelta(minutes=2))


MOVE = {"type": "moved", "object_label": "laptop", "actor_ref": None, "frame_index": 0, "confidence": 0.8}


def test_live_case_move_without_a_destination_is_dropped(make_reconciler, inv):
    _laptop(inv, "USOS")
    p = process(make_reconciler(HANDHELD, scorer=lambda f: 0.95), [obj("laptop", BOX_A)], hint=None,
                frames=[[("laptop-A", BOX_A)]], labels={"laptop-A": "laptop"},
                events=[{**MOVE, "to_location_hint": "somewhere"}])
    assert moved(p) == []


def test_move_to_the_own_segment_is_dropped(make_reconciler, inv):
    _laptop(inv, "LAB1")
    p = process(make_reconciler(HANDHELD, scorer=lambda f: 0.95), [obj("laptop", BOX_A)], hint="Destiny",
                frames=[[("laptop-A", BOX_A)]], labels={"laptop-A": "laptop"},
                events=[{**MOVE, "to_location_hint": "US Orbital Segment"}])
    assert moved(p) == []


def test_real_move_to_another_module_is_kept(make_reconciler, inv):
    _laptop(inv, "LAB1")
    p = process(make_reconciler(HANDHELD, scorer=lambda f: 0.95), [obj("laptop", BOX_A)], hint="Destiny",
                frames=[[("laptop-A", BOX_A)]], labels={"laptop-A": "laptop"},
                events=[{**MOVE, "to_location_hint": "Columbus"}])
    [m] = moved(p)
    assert (m["from_location"], m["to_location"]) == ("LAB1", "COL1")


def test_speech_move_inside_the_same_place_is_dropped(make_reconciler, inv):
    inv.add_entity(catalog_id="drill-pistol", last_location="NOD2D4", last_seen_at=T0 - timedelta(hours=1))
    p = process(make_reconciler(), [], hint=None, transcript="I'm putting the drill in node 2.")
    assert moved(p) == [] and inv.patches == []
