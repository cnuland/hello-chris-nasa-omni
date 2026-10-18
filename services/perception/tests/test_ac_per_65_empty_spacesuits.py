"""AC-PER-65 empty spacesuits are objects (UI review round 6, finding UI6-04).

Live: 3 of 18 person tracks were stowed EMUs (4ce46008 "white suit, blue gloves", 1b59a01c "spacesuit with black and
grey components", 91672559 "astronaut in white spacesuit"), each also counted by "how many people" and raising a
"more tracks than expected crew" review. The appearance/activity strings below are verbatim from the live replies
(tests/fixtures/gemma4_tour_two_pass_2026-10-18.json)."""
from __future__ import annotations

import json
from datetime import timedelta
from pathlib import Path

import pytest
from support import T0, detections_for, obj, observation, scene_window

from perception.people import is_empty_suit
from perception.types import OmniResult

HANDHELD = "cam-handheld"
FIXTURE = Path(__file__).parent / "fixtures" / "gemma4_tour_two_pass_2026-10-18.json"

LIVE_SUITS = [
    ("white suit, blue gloves", "working in the background module"),
    ("spacesuit with black and grey components", "standing in the hatchway"),
    ("astronaut in white spacesuit", "floating near the hatch"),
    ("astronaut in white spacesuit", "positioned inside the hatch area"),
    ("white suit, reaching forward", "reaching through a hatch"),
    ("white suit", "reaching through a hatch"),
    ("white extravehicular activity suit", None),
    ("EMU", ""),
    ("Orlan suit", None),
]
LIVE_CREW = [
    ("blue flight suit with American flag patch", "floating"),
    ("blue suit, orange gloves", "working at a rack"),
    ("person in grey suit", "floating"),
    ("blue shirt, orange helmet", "looking at the camera"),
    ("blue t-shirt, tan pants", "preparing equipment near spacesuits"),
    ("grey shirt, black shorts, headset", "floating"),
    ("dark hair, white suit", "floating"),
    ("white suit, smiling", "floating"),
    ("spacesuit, short hair", "talking to the camera"),
    ("white suit", "holding a camera"),
]


def person(ref, appearance, activity="floating", speaking=False):
    return {"ref": ref, "appearance": appearance, "activity": activity, "is_speaking": speaking}


def process(rec, objects=(), people=(), *, hint="Columbus", t=0.0, frames=None, labels=None):
    frames = frames if frames is not None else [[]]
    w = scene_window(frames, t0=T0 + timedelta(seconds=t))
    omni = OmniResult(status="ok", observation=observation(objects, people=people, location_hint=hint),
                      model="fake-omni", raw="{}")
    out = rec.process(w, omni, detections_for(frames, labels or {}))
    assert out.status == "posted", out.error
    return out.payload


def kinds(p):
    return [(a.get("new_entity") or {}).get("kind") for a in p["assignments"]]


@pytest.mark.parametrize(("appearance", "activity"), LIVE_SUITS)
def test_live_suit_strings_are_empty_suits(appearance, activity):
    assert is_empty_suit(appearance, activity, False)


@pytest.mark.parametrize(("appearance", "activity"), LIVE_CREW)
def test_crew_strings_stay_people(appearance, activity):
    assert not is_empty_suit(appearance, activity, False)


def test_a_speaking_suit_is_a_person():
    assert not is_empty_suit("astronaut in white spacesuit", "floating", True)


def test_live_case_suit_entry_becomes_one_unknown_spacesuit_not_a_person_track(make_reconciler, inv):
    p = process(make_reconciler(HANDHELD), people=[person("P1", "white suit, blue gloves",
                                                          "working in the background module")])
    [a] = p["assignments"]
    assert a["new_entity"]["kind"] == "object" and a["new_entity"]["status"] == "unknown"
    assert a["new_entity"]["label"] == "spacesuit"
    assert a["new_entity"]["description"] == "white suit, blue gloves"
    [r] = p["reviews"]
    assert r["kind"] == "unknown_object" and r["model_guess"] == "spacesuit"
    assert p["window"]["suits_as_objects"] == 1


def test_live_case_window_075305_suits_and_a_real_crew_member(make_reconciler, inv):
    """Two omni "spacesuit" listings plus a suit in people[] and the crew member preparing equipment near them: one
    person track, the suits are objects (the people[] suit consolidates with an omni listing)."""
    objects = [obj("spacesuit", (0.1, 0.1, 0.3, 0.6), description="white extravehicular activity suit"),
               obj("spacesuit", (0.6, 0.1, 0.8, 0.6), description="another white extravehicular activity suit")]
    people = [person("P1", "blue t-shirt, tan pants", "preparing equipment near spacesuits"),
              person("P2", "astronaut in white spacesuit", "floating near the hatch")]
    p = process(make_reconciler(HANDHELD), objects, people)
    assert kinds(p).count("person") == 1
    persons = [a for a in p["assignments"] if a["new_entity"]["kind"] == "person"]
    assert persons[0]["new_entity"]["description"] == "blue t-shirt, tan pants"
    suits = [a for a in p["assignments"] if a["new_entity"]["label"] == "spacesuit"]
    assert len(suits) == 2


def test_suit_seen_again_attaches_to_the_same_unknown(make_reconciler, inv):
    rec = make_reconciler(HANDHELD)
    p1 = process(rec, people=[person("P1", "spacesuit with black and grey components", "standing in the hatchway")])
    p2 = process(rec, people=[person("P1", "astronaut in white spacesuit", "floating near the hatch")], t=30)
    assert p1["assignments"][0]["entity_id"] is None
    assert p2["assignments"][0]["entity_id"] is not None and p2["reviews"] == []
    assert [e for e in inv.entities.values() if e["kind"] == "person"] == []


def test_person_object_described_as_an_empty_suit_becomes_an_object_without_a_box(make_reconciler, inv):
    """Review round: the box is dropped (it may still frame a suited crew member, so no crop of it is ever made)."""
    box = (0.2, 0.2, 0.5, 0.8)
    p = process(make_reconciler(HANDHELD), [obj("person", box, description="astronaut in white spacesuit")])
    [a] = p["assignments"]
    assert a["new_entity"]["kind"] == "object" and a["new_entity"]["label"] == "spacesuit"
    assert a["sighting"]["bbox"] is None and a["exemplar"] is None


def test_catalog_spacesuit_class_is_used_when_present(make_reconciler, inv):
    from support import CATALOG

    catalog = [*CATALOG, {"catalog_id": "emu", "class_name": "spacesuit", "display_name": "EMU spacesuit",
                          "synonyms": ["EMU", "space suit"], "category": "object", "is_fungible": False,
                          "expected_count": 4, "home_location": None}]
    from perception.reconciler import Reconciler

    rec = make_reconciler(HANDHELD)
    rec._set_catalog(catalog, rec.locations)
    p = process(rec, people=[person("P1", "white suit, blue gloves", "working in the background module")])
    [a] = p["assignments"]
    assert a["new_entity"]["catalog_id"] == "emu" and a["new_entity"]["kind"] == "object"
    assert isinstance(rec, Reconciler)


def test_crew_member_in_a_blue_suit_is_still_a_person(make_reconciler, inv):
    p = process(make_reconciler(HANDHELD), people=[person("P1", "blue suit, orange gloves", "working at a rack")])
    assert kinds(p) == ["person"] and p["window"]["suits_as_objects"] == 0


def test_private_window_suit_is_skipped_like_any_unknown(make_reconciler, inv):
    p = process(make_reconciler(HANDHELD), people=[person("P1", "white suit, blue gloves", "")], hint="crew quarters")
    assert p["assignments"] == [] and p["window"]["private"] is True


def test_suit_entries_do_not_count_as_people_for_the_query_fail_closed(make_reconciler, inv):
    """AC-PER-55: omni reporting a person but no person box blocks query boxes; an empty suit is not a person."""
    from perception.types import Detection

    frames = [[]]
    w = scene_window(frames)
    omni = OmniResult(status="ok", observation=observation(
        [obj("orange sphere", None, description="a small orange ball")],
        people=[person("P1", "white suit, blue gloves", "")], location_hint="Kibo"), model="fake-omni", raw="{}")
    rec = make_reconciler(HANDHELD)
    out = rec.process(w, omni, [[]], query_detect=lambda f, texts: [Detection("orange sphere", (0.4, 0.4, 0.5, 0.5),
                                                                              0.6)])
    sphere = next(a for a in out.payload["assignments"] if a["new_entity"]["label"] == "orange sphere")
    assert sphere["sighting"]["bbox_source"] == "detector"


def test_fixture_people_replay_has_no_suit_person_tracks(make_reconciler, inv):
    """Every people[] entry of the live two-pass run: none of the suit strings becomes a person track."""
    data = json.loads(FIXTURE.read_text())
    windows = data["windows"] if isinstance(data, dict) else data
    rec = make_reconciler(HANDHELD)
    t = 0.0
    for w in windows:
        if w.get("private") or not w.get("people"):
            continue
        people = [person(p.get("ref") or f"P{i}", p.get("appearance") or "", p.get("activity") or "",
                         bool(p.get("is_speaking"))) for i, p in enumerate(w["people"])]
        process(rec, people=people, hint=None, t=t)
        t += 30
    tracks = [e for e in inv.entities.values() if e["kind"] == "person"]
    suits = [e for e in tracks if is_empty_suit(e["description"], "", False)]
    assert tracks and suits == []
    assert any(e["label"] == "spacesuit" for e in inv.entities.values())


def test_a_suit_that_is_an_event_actor_is_a_person(make_reconciler, inv):
    """"spacesuit, dark visor" with no activity is an empty suit, unless omni says it handled something."""
    p = process(make_reconciler(HANDHELD), people=[person("P1", "spacesuit, dark visor", None)])
    assert kinds(p) == ["object"]
    w = scene_window([[]])
    omni = OmniResult(status="ok", observation=observation(
        [obj("laptop", None)], people=[person("P1", "spacesuit, dark visor", None)], location_hint="Columbus",
        events=[{"type": "used", "object_label": "laptop", "actor_ref": "P1", "confidence": 0.8}]),
        model="fake-omni", raw="{}")
    p = make_reconciler(HANDHELD).process(w, omni, [[]]).payload
    assert sorted(kinds(p)) == ["object", "person"] and p["window"]["suits_as_objects"] == 0


def test_live_case_a_suit_moving_itself_is_still_an_empty_suit(make_reconciler, inv):
    """07:56:30: "spacesuit with black and grey components", event moved / object "person" / actor P1 ("the person in
    the spacesuit shifts position slightly within the hatchway"). A self-move is no handling; an arm or hand moving
    (08:02:00, 08:17:00) is a living person and stays one."""
    ev = {"type": "moved", "object_label": "person", "actor_ref": "P1", "confidence": 0.7, "frame_index": 0}
    w = scene_window([[]])
    omni = OmniResult(status="ok", observation=observation(
        [], people=[person("P1", "spacesuit with black and grey components", "standing in the hatchway")],
        events=[ev], location_hint="Columbus"), model="fake-omni", raw="{}")
    p = make_reconciler(HANDHELD).process(w, omni, [[]]).payload
    assert kinds(p) == ["object"] and p["window"]["suits_as_objects"] == 1
    arm = {**ev, "object_label": "arm"}
    omni = OmniResult(status="ok", observation=observation(
        [], people=[person("P1", "white suit, reaching forward", "reaching through a hatch")], events=[arm],
        location_hint="Columbus"), model="fake-omni", raw="{}")
    p = make_reconciler(HANDHELD).process(scene_window([[]]), omni, [[]]).payload
    assert kinds(p) == ["person"]
