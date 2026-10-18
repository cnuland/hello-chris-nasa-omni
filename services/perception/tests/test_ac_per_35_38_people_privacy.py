"""AC-PER-35 person tracks from people[] (Gemma 4 reports people only there), AC-PER-36 person privacy (no crops, no
exemplars, nothing in no_capture windows), AC-PER-37 sleep cues make a window no_capture.

Real Gemma 4 12B appearance strings from the live ISS tour run (2026-10-18) are used verbatim."""
from __future__ import annotations

import logging
from datetime import timedelta

import pytest
from support import CATALOG, CATALOG_BY_ID, LOCATIONS, T0, detections_for, obj, observation, scene_window

from perception.types import OmniResult

LAB_CAM = "cam-lab1-fwd"  # LAB1
HARMONY_CAM = "cam-nod2-aft"  # NOD2 Harmony, the module with the crew quarters
BOX_A = (0.05, 0.10, 0.40, 0.90)
BOX_B = (0.55, 0.10, 0.95, 0.90)
BOX_L = (0.42, 0.60, 0.52, 0.75)  # a laptop
PERSON_CLASS = "crew-member"
PERSON_LABEL = CATALOG_BY_ID[PERSON_CLASS]["class_name"]

GEMMA_FLIGHT_SUIT = "blue flight suit with American flag patch"
GEMMA_BLACK_SHIRT = "black shirt, grey pants"
GEMMA_BLUE_SHIRT = "blue shirt, dark hair"
GEMMA_SHORTS = "dark hair, dark t-shirt, blue shorts"
GEMMA_SLEEPING = "dark hair, sleeping in a blue sleeping bag"


def person(ref: str, appearance: str, activity: str | None = None) -> dict:
    p = {"ref": ref, "appearance": appearance}
    if activity is not None:
        p["activity"] = activity
    return p


def run(rec, *, people=(), objects=(), labels=None, frames=None, t=0.0, summary="crew working in the module",
        events=(), hint=None, transcript=""):
    frames = frames if frames is not None else [[]]
    w = scene_window(frames, t0=T0 + timedelta(seconds=t))
    omni = OmniResult(status="ok", observation=observation(objects, people=people, events=events,
                                                           scene_summary=summary, location_hint=hint,
                                                           transcript=transcript),
                      model="fake-omni", raw='{"raw": "reply"}')
    out = rec.process(w, omni, detections_for(frames, labels or {}))
    assert out.status == "posted", out.error
    return out.payload, out.response


def person_assignments(payload, inv) -> list[dict]:
    out = []
    for a in payload["assignments"]:
        ne = a.get("new_entity")
        if (ne and ne.get("kind") == "person") or (a.get("entity_id") and inv.entities[a["entity_id"]]["kind"] == "person"):
            out.append(a)
    return out


def entity_of(a, response) -> str:
    return a["entity_id"] or response["entities"][a["ref"]]


def live_people(inv) -> list[dict]:
    return [e for e in inv.live() if e["kind"] == "person"]


def seed_person(inv, description, *, location="LAB1", ago_s=60.0):
    return inv.add_entity(catalog_id=PERSON_CLASS, kind="person", description=description, last_location=location,
                          last_seen_at=T0 - timedelta(seconds=ago_s))


# ------------------------------------------------------------------------------------------------ AC-PER-35 similarity
@pytest.mark.parametrize("a,b", [
    ("crew member, blue shirt", "crew member, red shirt"),  # same garment, different colour
    (GEMMA_BLACK_SHIRT, GEMMA_BLUE_SHIRT),
    ("dark hair, black shirt", "dark hair, blue shorts"),  # only the (weak) hair matches
    ("brown hair, white polo", "black hair, white polo"),
    ("", "blue shirt"),
])
def test_ac_per_35_appearance_similarity_keeps_different_people_apart(a, b):
    from perception.people import appearance_similarity

    assert appearance_similarity(a, b) < 0.5
    assert appearance_similarity(b, a) == appearance_similarity(a, b)


@pytest.mark.parametrize("a,b", [
    (GEMMA_FLIGHT_SUIT, "astronaut in a blue flight suit"),
    (GEMMA_SHORTS, "dark hair, black t-shirt"),  # "dark" is compatible with black
    ("crew member, blue shirt", "blue shirt, khaki pants"),
    ("grey trousers, navy tee", "gray pants, blue t-shirt"),  # synonyms and colour families
    ("dark hair", "dark hair, black shirt"),  # more detail later
    ("crew member, blue shirt", "crew member, blue shirt"),
])
def test_ac_per_35_appearance_similarity_matches_the_same_person(a, b):
    from perception.people import appearance_similarity

    assert appearance_similarity(a, b) >= 0.5


def test_ac_per_35_appearance_descriptors_ignore_generic_and_activity_words():
    from perception.people import appearance_descriptors

    assert appearance_descriptors("astronaut, crew member, person floating near the rack") == {}
    d = appearance_descriptors(GEMMA_FLIGHT_SUIT)
    assert d["suit"] == {"blue"} and "patch" in d and "flag" in d


def test_ac_per_35_sanitise_text_strips_controls_and_caps_length():
    from perception.people import sanitise_text

    assert sanitise_text("blue shirt,\n\x00 headset \u200b  ") == "blue shirt, headset"
    assert len(sanitise_text("x" * 500)) == 200
    assert sanitise_text(None) == ""


# ------------------------------------------------------------------------------------------------ AC-PER-35 tracks
def test_ac_per_35_people_only_window_creates_person_tracks(make_reconciler, inv):
    payload, _ = run(make_reconciler(LAB_CAM),
                     people=[person("P1", GEMMA_FLIGHT_SUIT), person("P2", GEMMA_BLACK_SHIRT, "working at a rack")])
    people = person_assignments(payload, inv)
    assert len(people) == 2
    assert len({a["ref"] for a in payload["assignments"]}) == len(payload["assignments"])
    for a, text in zip(people, (GEMMA_FLIGHT_SUIT, GEMMA_BLACK_SHIRT), strict=True):
        ne = a["new_entity"]
        assert ne["catalog_id"] == PERSON_CLASS and ne["kind"] == "person" and ne["status"] == "provisional"
        assert ne["label"] == PERSON_LABEL and ne["description"] == text
        assert a["sighting"]["description"] == text and a["sighting"]["location_id"] == "LAB1"
        assert a["exemplar"] is None and a["sighting"]["bbox"] is None
    assert len(live_people(inv)) == 2


def test_ac_per_35_two_similar_people_in_one_window_stay_separate(make_reconciler, inv):
    seed_person(inv, "dark hair, blue shirt")
    payload, response = run(make_reconciler(LAB_CAM),
                            people=[person("P1", "dark hair, blue shirt"), person("P2", "dark hair, blue shirt")])
    people = person_assignments(payload, inv)
    assert len(people) == 2
    assert len({entity_of(a, response) for a in people}) == 2  # one attaches, the other is a new track


def test_ac_per_35_same_person_next_window_attaches(make_reconciler, inv):
    rec = make_reconciler(LAB_CAM)
    p1, r1 = run(rec, people=[person("P1", GEMMA_FLIGHT_SUIT)])
    p2, _ = run(rec, people=[person("P1", "astronaut in a blue flight suit")], t=8)
    first, second = person_assignments(p1, inv)[0], person_assignments(p2, inv)[0]
    assert second["entity_id"] == entity_of(first, r1)
    assert "new_entity" not in second
    assert len(live_people(inv)) == 1


def test_ac_per_35_different_outfit_is_a_new_track(make_reconciler, inv):
    rec = make_reconciler(LAB_CAM)
    run(rec, people=[person("P1", GEMMA_BLUE_SHIRT)])
    p2, _ = run(rec, people=[person("P1", GEMMA_BLACK_SHIRT)], t=8)
    assert person_assignments(p2, inv)[0]["entity_id"] is None
    assert len(live_people(inv)) == 2


def test_ac_per_35_two_known_people_are_not_swapped(make_reconciler, inv):
    blue = seed_person(inv, "crew member, blue shirt", ago_s=30)
    red = seed_person(inv, "crew member, red shirt", ago_s=60)
    payload, _ = run(make_reconciler(LAB_CAM), people=[person("P1", "red shirt, headset"), person("P2", "blue shirt")])
    by_desc = {a["sighting"]["description"]: a["entity_id"] for a in person_assignments(payload, inv)}
    assert by_desc == {"red shirt, headset": red, "blue shirt": blue}


def test_ac_per_35_person_in_another_module_is_not_attached(make_reconciler, inv):
    seed_person(inv, GEMMA_FLIGHT_SUIT, location="NOD2")
    payload, _ = run(make_reconciler(LAB_CAM), people=[person("P1", GEMMA_FLIGHT_SUIT)])
    assert person_assignments(payload, inv)[0]["entity_id"] is None


def test_ac_per_35_revisit_after_a_long_gap_attaches_by_appearance(make_reconciler, inv):
    eid = seed_person(inv, GEMMA_FLIGHT_SUIT, ago_s=3600)
    seed_person(inv, GEMMA_BLACK_SHIRT, ago_s=3000)
    payload, _ = run(make_reconciler(LAB_CAM), people=[person("P1", "blue flight suit")])
    assert person_assignments(payload, inv)[0]["entity_id"] == eid


def test_ac_per_35_descriptorless_appearance_attaches_to_the_most_recent_person(make_reconciler, inv):
    recent = seed_person(inv, GEMMA_BLUE_SHIRT, ago_s=20)
    seed_person(inv, GEMMA_BLACK_SHIRT, ago_s=200)
    payload, _ = run(make_reconciler(LAB_CAM), people=[person("P1", "astronaut")])
    assert person_assignments(payload, inv)[0]["entity_id"] == recent


def test_ac_per_35_person_object_and_people_entry_are_one_sighting(make_reconciler, inv, storage):
    frames = [[("astro-1", BOX_A)]]
    payload, _ = run(make_reconciler(LAB_CAM), frames=frames, labels={"astro-1": "astronaut"},
                     objects=[obj("astronaut", BOX_A, description="astronaut floating")],
                     people=[person("P1", "blue shirt, headset")])
    people = person_assignments(payload, inv)
    assert len(people) == 1 and len(payload["assignments"]) == 1
    a = people[0]
    assert a["sighting"]["bbox"] == pytest.approx(list(BOX_A), abs=0.01)
    assert a["sighting"]["bbox_source"] == "detector"
    assert a["sighting"]["description"] == "blue shirt, headset"
    assert a["new_entity"]["description"] == "blue shirt, headset" and a["new_entity"]["kind"] == "person"


def test_ac_per_35_person_objects_are_paired_with_entries_by_appearance(make_reconciler, inv):
    frames = [[("astro-1", BOX_A), ("astro-2", BOX_B)]]
    payload, _ = run(make_reconciler(LAB_CAM), frames=frames, labels={"astro-1": "astronaut", "astro-2": "astronaut"},
                     objects=[obj("astronaut", BOX_A, description="astronaut in a red shirt"),
                              obj("astronaut", BOX_B, description="astronaut in a blue shirt")],
                     people=[person("P1", "blue shirt"), person("P2", "red shirt")])
    people = person_assignments(payload, inv)
    assert len(people) == 2
    box = {a["sighting"]["description"]: a["sighting"]["bbox"] for a in people}
    assert box["blue shirt"] == pytest.approx(list(BOX_B), abs=0.01)
    assert box["red shirt"] == pytest.approx(list(BOX_A), abs=0.01)


def test_ac_per_35_ambiguous_leftover_pairs_drop_the_box(make_reconciler, inv):
    frames = [[("astro-1", BOX_A), ("astro-2", BOX_B)]]
    payload, _ = run(make_reconciler(LAB_CAM), frames=frames, labels={"astro-1": "astronaut", "astro-2": "astronaut"},
                     objects=[obj("astronaut", BOX_A, description="astronaut"),
                              obj("astronaut", BOX_B, description="astronaut")],
                     people=[person("P1", "blue shirt"), person("P2", "red shirt")])
    people = person_assignments(payload, inv)
    assert len(people) == 2 and len(payload["assignments"]) == 2
    assert all(a["sighting"]["bbox"] is None for a in people)


def test_ac_per_35_event_actor_desc_comes_from_people_and_is_sanitised(make_reconciler, inv):
    frames = [[("laptop-1", BOX_L)]]
    ev = {"type": "used", "object_label": "laptop", "actor_ref": "P1", "frame_index": 0, "confidence": 0.8}
    payload, _ = run(make_reconciler(LAB_CAM), frames=frames, labels={"laptop-1": "laptop"},
                     objects=[obj("laptop", BOX_L)], people=[person("P1", "blue shirt,\n\x00 headset  ")], events=[ev])
    assert payload["events"][0]["actor_desc"] == "blue shirt, headset"
    assert person_assignments(payload, inv)[0]["new_entity"]["description"] == "blue shirt, headset"


def test_ac_per_35_people_ignored_without_a_person_class(inv, storage, embedder, config):
    from perception.fakes import FakeDecision
    from perception.reconciler import Reconciler

    catalog = [c for c in CATALOG if c.get("category") != "person"]
    rec = Reconciler(api=inv, embedder=embedder, decision=FakeDecision(), storage=storage, config=config,
                     camera_id=LAB_CAM, catalog=catalog, locations=LOCATIONS)
    payload, _ = run(rec, people=[person("P1", GEMMA_FLIGHT_SUIT)])
    assert payload["assignments"] == []


def test_ac_per_35_person_match_floor_config():
    from perception.config import ConfigError, PerceptionConfig

    assert PerceptionConfig().person_match_floor == 0.5
    assert PerceptionConfig.from_env({"PERCEPTION_PERSON_MATCH_FLOOR": "0.7"}).person_match_floor == 0.7
    with pytest.raises(ConfigError):
        PerceptionConfig.from_env({"PERCEPTION_PERSON_MATCH_FLOOR": "1.5"})


# ------------------------------------------------------------------------------------------------ AC-PER-36 privacy
def test_ac_per_36_person_object_never_gets_a_crop_or_exemplar(make_reconciler, inv, storage):
    frames = [[("astro-1", BOX_A)]]
    payload, _ = run(make_reconciler(LAB_CAM), frames=frames, labels={"astro-1": "astronaut"},
                     objects=[obj("astronaut", BOX_A, description="astronaut in a blue shirt")])
    a = person_assignments(payload, inv)[0]
    assert a["exemplar"] is None
    assert not [k for k in storage.objects if k.startswith("crops/")], list(storage.objects)
    assert a["sighting"].get("frame_uri")  # capture window: the frame itself is stored as for every window
    assert "candidates" not in inv.calls  # never compared by an appearance embedding


def test_ac_per_36_no_capture_window_records_no_person_and_no_actor(make_reconciler, inv, storage):
    frames = [[("laptop-1", BOX_L)]]
    ev = {"type": "used", "object_label": "laptop", "actor_ref": "P1", "frame_index": 0, "confidence": 0.8}
    payload, _ = run(make_reconciler("cam-handheld"), frames=frames, labels={"laptop-1": "laptop"}, hint="NOD2S5",
                     objects=[obj("laptop", BOX_L), obj("astronaut", BOX_A)],
                     people=[person("P1", GEMMA_BLUE_SHIRT)], events=[ev])
    assert person_assignments(payload, inv) == []
    assert [a["new_entity"]["catalog_id"] for a in payload["assignments"]] == ["laptop"]
    assert all(e["actor_desc"] is None for e in payload["events"])
    assert storage.objects == {}


# ------------------------------------------------------------------------------------------------ AC-PER-37 sleep cues
def test_ac_per_37_real_gemma_sleeping_crew_window_is_no_capture(make_reconciler, inv, storage, caplog):
    frames = [[("laptop-1", BOX_L)]]
    caplog.set_level(logging.INFO, logger="perception.reconciler")
    payload, _ = run(make_reconciler(HARMONY_CAM), frames=frames, labels={"laptop-1": "laptop"},
                     objects=[obj("laptop", BOX_L), obj("glowing orb", BOX_B)],
                     people=[person("P1", GEMMA_SHORTS, "floating"), person("P2", GEMMA_SLEEPING)],
                     summary="Two crew members in the Harmony module", transcript="good night everyone",
                     hint="Harmony")
    assert storage.objects == {}, list(storage.objects)
    w = payload["window"]
    assert w.get("raw_uri") is None and w["transcript"] == ""
    assert payload["reviews"] == []
    assert person_assignments(payload, inv) == []
    assert [a["new_entity"]["catalog_id"] for a in payload["assignments"]] == ["laptop"]  # known object still sighted
    assert payload["assignments"][0]["sighting"].get("frame_uri") is None
    assert w["privacy"] == {"no_capture": True, "reason": "sleep_cue", "cue": "sleeping bag",
                            "field": "people[1].appearance"}
    rec = next(r for r in caplog.records if "sleep cue" in r.getMessage())
    assert rec.levelno == logging.INFO
    assert GEMMA_SLEEPING not in rec.getMessage() and GEMMA_SLEEPING not in str(rec.__dict__)


@pytest.mark.parametrize("where,text,field", [
    ("activity", "sleeping", "people[0].activity"),
    ("appearance", "crew member in a sleeping bag", "people[0].appearance"),
    ("appearance", "asleep, grey hoodie", "people[0].appearance"),
    ("label", "sleeping bag", "objects[0].label"),
    ("label", "Sleeping-Bags", "objects[0].label"),
    ("description", "a bunk with a pillow", "objects[0].description"),
    ("description", "inside a SLEEP STATION", "objects[0].description"),
    ("summary", "crew member asleep", "scene_summary"),
    ("summary", "the bunks of the crew", "scene_summary"),
])
def test_ac_per_37_sleep_cues_in_every_field_make_the_window_private(make_reconciler, inv, storage, where, text,
                                                                     field):
    frames = [[("laptop-1", BOX_L)]]
    objects = [obj("laptop", BOX_L)]
    people = [person("P1", "blue shirt")]
    summary = "crew working in the module"
    if where == "activity":
        people = [person("P1", "blue shirt", text)]
    elif where == "appearance":
        people = [person("P1", text)]
    elif where == "label":
        objects = [obj(text, BOX_B, description="a bag"), obj("laptop", BOX_L)]
    elif where == "description":
        objects = [obj("pillow", BOX_B, description=text), obj("laptop", BOX_L)]
    else:
        summary = text
    payload, _ = run(make_reconciler(HARMONY_CAM), frames=frames, labels={"laptop-1": "laptop"}, objects=objects,
                     people=people, summary=summary)
    assert storage.objects == {}
    assert payload["window"]["privacy"]["reason"] == "sleep_cue"
    assert payload["window"]["privacy"]["field"] == field


@pytest.mark.parametrize("text", ["blue shirt with rolled-up sleeves", "cargo bunker", "crew member working at a rack",
                                  "sleep mask in a pouch"])
def test_ac_per_37_no_sleep_cue_keeps_the_window_capturable(make_reconciler, inv, storage, text):
    frames = [[("laptop-1", BOX_L)]]
    payload, _ = run(make_reconciler(HARMONY_CAM), frames=frames, labels={"laptop-1": "laptop"},
                     objects=[obj("laptop", BOX_L, description=text)], people=[person("P1", text)], summary=text)
    assert "privacy" not in payload["window"]
    assert payload["window"].get("raw_uri")
    assert person_assignments(payload, inv)


@pytest.mark.parametrize("kwargs,reason", [
    ({"hint": "NOD2S5"}, "location"),
    ({"summary": "view into the crew quarters"}, "scene_summary"),
])
def test_ac_per_37_other_private_reasons_are_recorded(make_reconciler, inv, storage, kwargs, reason):
    payload, _ = run(make_reconciler("cam-handheld"), **kwargs)
    assert payload["window"]["privacy"]["no_capture"] is True
    assert payload["window"]["privacy"]["reason"] == reason


# ================================================================================ adversarial review round (2026-10-18)
@pytest.mark.parametrize("text", [
    "dark hair, in a blue sleepingbag",  # one word
    "slee\u00adping in a bag",  # soft hyphen inside the word
    "sleep\u200bing crew member",  # zero-width space inside the word
    "crew member sleeps in the corner", "napping", "dozing off", "in a sleep pod", "slumbering astronaut",
])
def test_review_sleep_cue_bypasses_are_caught(text):
    from perception.people import sleep_cue

    assert sleep_cue(text) is not None


def test_review_sanitise_deletes_invisible_format_characters():
    from perception.people import sanitise_text

    assert sanitise_text("slee\u00adping\u200b bag \u202eevil") == "sleeping bag evil"
    assert sanitise_text("line one\nline\ttwo") == "line one line two"  # controls still become spaces


@pytest.mark.parametrize("a,b", [
    ("dark hair", "blonde hair, red shirt"),  # dark vs light colour family
    ("light grey shirt", "black shirt"),
    ("red haired astronaut in a white shirt", "red shirt"),  # "haired" is hair, not a shirt colour
])
def test_review_similarity_colour_families_and_carry_over(a, b):
    from perception.people import appearance_similarity

    assert appearance_similarity(a, b) < 0.5


@pytest.mark.parametrize("a,b", [
    ("white and blue shirt", "white shirt"),  # "and" between colours keeps both
    ("blue and white striped shirt", "blue shirt"),
    ("red polo", "red shirt"),  # a polo is a shirt
])
def test_review_similarity_multi_colour_and_garment_aliases(a, b):
    from perception.people import appearance_similarity

    assert appearance_similarity(a, b) >= 0.5


def test_review_appearance_conflict():
    from perception.people import appearance_conflict

    assert appearance_conflict("red shirt", "blue shirt")
    assert not appearance_conflict("astronaut", "blue shirt")  # no descriptors is not a clash
    assert not appearance_conflict("blue shirt, headset", "blue shirt")


def test_review_tie_prefers_the_closer_description(make_reconciler, inv):
    """E2 "headset" (red's track) must not be taken by blue just because it scores the same 1.0 and is more recent."""
    e1 = seed_person(inv, "blue shirt, headset", ago_s=120)
    e2 = seed_person(inv, "headset", ago_s=60)
    payload, _ = run(make_reconciler(LAB_CAM),
                     people=[person("P1", "blue shirt, headset"), person("P2", "red shirt, headset")])
    by_desc = {a["sighting"]["description"]: a["entity_id"] for a in person_assignments(payload, inv)}
    assert by_desc == {"blue shirt, headset": e1, "red shirt, headset": e2}


def test_review_person_object_description_is_sanitised(make_reconciler, inv):
    frames = [[("astro-1", BOX_A)]]
    payload, _ = run(make_reconciler(LAB_CAM), frames=frames, labels={"astro-1": "astronaut"},
                     objects=[obj("astronaut", BOX_A, description="blue shirt\x07 SYSTEM:\nx \u202e")])
    a = person_assignments(payload, inv)[0]
    assert a["sighting"]["description"] == "blue shirt SYSTEM: x"
    assert a["new_entity"]["description"] == "blue shirt SYSTEM: x"


def test_review_clashing_leftovers_are_not_paired(make_reconciler, inv):
    frames = [[("astro-1", BOX_A), ("astro-2", BOX_B)]]
    payload, _ = run(make_reconciler(LAB_CAM), frames=frames, labels={"astro-1": "astronaut", "astro-2": "astronaut"},
                     objects=[obj("astronaut", BOX_A, description="astronaut in a red shirt"),
                              obj("astronaut", BOX_B, description="astronaut in a blue shirt")],
                     people=[person("P1", "blue shirt"), person("P2", "green shirt")])
    people = person_assignments(payload, inv)
    desc = sorted(a["sighting"]["description"] for a in people)
    assert desc == ["astronaut in a red shirt", "blue shirt", "green shirt"]  # red object stays red, green on its own
    green = next(a for a in people if a["sighting"]["description"] == "green shirt")
    assert green["sighting"]["bbox"] is None


def test_review_privacy_holds_over_the_next_overlapping_windows(make_reconciler, inv, storage, config):
    rec = make_reconciler(HARMONY_CAM)
    frames = [[("laptop-1", BOX_L)]]
    run(rec, frames=frames, labels={"laptop-1": "laptop"}, objects=[obj("laptop", BOX_L)],
        people=[person("P1", GEMMA_SLEEPING)])
    p2, _ = run(rec, frames=frames, labels={"laptop-1": "laptop"}, objects=[obj("laptop", BOX_L)],
                people=[person("P1", "person resting in a blue bag, dark hair")], t=4)
    assert storage.objects == {}
    assert p2["window"]["privacy"]["reason"] == "privacy_hold"
    assert person_assignments(p2, inv) == []
    p3, _ = run(rec, frames=frames, labels={"laptop-1": "laptop"}, objects=[obj("laptop", BOX_L)],
                people=[person("P1", "blue shirt")], t=4 + config.privacy_hold_s + 1)
    assert "privacy" not in p3["window"] and p3["window"].get("raw_uri")


def test_review_privacy_hold_config():
    from perception.config import PerceptionConfig

    assert PerceptionConfig().privacy_hold_s == 30.0
    assert PerceptionConfig.from_env({"PERCEPTION_PRIVACY_HOLD_S": "0"}).privacy_hold_s == 0.0
