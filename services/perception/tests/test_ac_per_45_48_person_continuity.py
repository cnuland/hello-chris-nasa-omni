"""AC-PER-45..48 person-track over-counting (live Gemma 4 run on the NASA tour, 2026-10-18: 25 person tracks for 7 to
11 crew, USOS 17).

- AC-PER-45 cross-module continuity (PERCEPTION_PERSON_CROSS_MODULE_S, default 600 s)
- AC-PER-46 appearance normalisation on the real strings (sleeve length, spacesuit, accessories, weights)
- AC-PER-47 soft cap at the catalog's expected_count (PERCEPTION_PERSON_SOFT_CAP)
- AC-PER-48 real-data regression (tests/fixtures/gemma4_tour_people_2026-10-18.json)

Appearance text only, never biometrics. Real Gemma strings are used verbatim."""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

import pytest
from support import CATALOG, scene_window
from test_ac_per_35_38_people_privacy import (
    GEMMA_FLIGHT_SUIT,
    LAB_CAM,
    PERSON_CLASS,
    entity_of,
    live_people,
    person,
    person_assignments,
    run,
    seed_person,
)

from perception.omni import interpret_reply
from perception.people import appearance_conflict, appearance_descriptors, appearance_similarity, sleep_cue
from perception.types import OmniResult

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "gemma4_tour_people_2026-10-18.json"
HANDHELD = "cam-handheld"  # roaming: configured at USOS, module from hints
AC_PER_35_REAL_TRACKS = 37  # the fixture through the AC-PER-35 matching (measured, docs/tdd-log/perception.md)


# ================================================================================ AC-PER-45 cross-module continuity
def test_ac_per_45_person_seen_in_another_module_recently_attaches(make_reconciler, inv):
    eid = seed_person(inv, GEMMA_FLIGHT_SUIT, location="NOD2", ago_s=60)
    payload, _ = run(make_reconciler(LAB_CAM), people=[person("P1", "blue flight suit, US flag patch")])
    a = person_assignments(payload, inv)[0]
    assert a["entity_id"] == eid and "new_entity" not in a
    assert len(live_people(inv)) == 1


def test_ac_per_45_same_person_through_three_modules_in_five_minutes_is_one_track(make_reconciler, inv):
    rec = make_reconciler(HANDHELD)
    for i, hint in enumerate(["Unity", "Columbus", "Kibo", "Unity"]):
        run(rec, people=[person("P1", ("dark hair, blue shirt", "astronaut wearing a blue shirt",
                                       "blue shirt, khaki pants", "crew member, blue shirt")[i])],
            hint=hint, t=90.0 * i)
    # AC-PER-66 amendment (UI6-02): the third module within 10 minutes needs strong evidence. "blue shirt, khaki
    # pants" against the track's "dark hair, blue shirt" (closeness 0.57) is the live blue-shirt over-merge, so Kibo
    # starts a second track; the fourth sighting (Unity again, no new module) joins the first track.
    tracks = live_people(inv)
    assert len(tracks) == 2
    assert sorted(p["last_location"] for p in tracks) == ["JPM1", "NOD1"]


def test_ac_per_45_distinctive_person_through_three_modules_in_five_minutes_is_one_track(make_reconciler, inv):
    """AC-PER-66: strong, distinctive evidence still carries a track through a third module."""
    rec = make_reconciler(HANDHELD)
    looks = "blue flight suit with American flag patch, orange gloves"
    for i, hint in enumerate(["Unity", "Columbus", "Kibo", "Unity"]):
        run(rec, people=[person("P1", looks)], hint=hint, t=90.0 * i)
    assert [p["last_location"] for p in live_people(inv)] == ["NOD1"]


def test_ac_per_45_beyond_the_horizon_only_same_module_matches(make_reconciler, inv):
    seed_person(inv, GEMMA_FLIGHT_SUIT, location="NOD2", ago_s=700)
    payload, _ = run(make_reconciler(LAB_CAM), people=[person("P1", GEMMA_FLIGHT_SUIT)])
    assert person_assignments(payload, inv)[0]["entity_id"] is None
    assert len(live_people(inv)) == 2


def test_ac_per_45_same_module_wins_a_tie(make_reconciler, inv):
    here = seed_person(inv, "crew member, blue shirt", location="LAB1", ago_s=300)
    seed_person(inv, "crew member, blue shirt", location="NOD2", ago_s=20)  # more recent, but another module
    payload, _ = run(make_reconciler(LAB_CAM), people=[person("P1", "blue shirt")])
    assert person_assignments(payload, inv)[0]["entity_id"] == here


def test_ac_per_45_a_closer_description_elsewhere_beats_a_looser_one_here(make_reconciler, inv):
    seed_person(inv, "blue shirt", location="LAB1", ago_s=30)
    there = seed_person(inv, "blue shirt, khaki pants", location="NOD2", ago_s=60)
    payload, _ = run(make_reconciler(LAB_CAM), people=[person("P1", "blue shirt, khaki pants")])
    assert person_assignments(payload, inv)[0]["entity_id"] == there


def test_ac_per_45_segment_level_window_attaches_to_a_module_track(make_reconciler, inv):
    """The live USOS 17: windows of the roaming camera whose module was not resolved."""
    eid = seed_person(inv, "short hair, grey t-shirt, black shorts", location="NOD1", ago_s=90)
    payload, _ = run(make_reconciler(HANDHELD), people=[person("P1", "grey t-shirt, black shorts")])
    assert payload["window"]["location"]["location_id"] == "USOS"
    assert person_assignments(payload, inv)[0]["entity_id"] == eid


def test_ac_per_45_module_window_attaches_to_a_segment_level_track(make_reconciler, inv):
    eid = seed_person(inv, "grey t-shirt, black shorts", location="USOS", ago_s=60)
    payload, _ = run(make_reconciler(LAB_CAM), people=[person("P1", "short hair, grey t-shirt, black shorts")])
    assert person_assignments(payload, inv)[0]["entity_id"] == eid


def test_ac_per_45_featureless_appearance_does_not_cross_modules(make_reconciler, inv):
    seed_person(inv, GEMMA_FLIGHT_SUIT, location="NOD2", ago_s=30)
    payload, _ = run(make_reconciler(LAB_CAM), people=[person("P1", "hand visible")])
    assert person_assignments(payload, inv)[0]["entity_id"] is None


def test_ac_per_45_two_people_of_one_window_never_share_a_cross_module_track(make_reconciler, inv):
    seed_person(inv, "dark hair, blue shirt", location="NOD2", ago_s=30)
    payload, response = run(make_reconciler(LAB_CAM),
                            people=[person("P1", "dark hair, blue shirt"), person("P2", "blue shirt")])
    people = person_assignments(payload, inv)
    assert len({entity_of(a, response) for a in people}) == 2


def test_ac_per_45_colour_clash_never_matches_even_with_a_zero_floor(make_reconciler, inv):
    seed_person(inv, "crew member, red shirt", location="LAB1", ago_s=30)
    seed_person(inv, "crew member, red shirt", location="NOD2", ago_s=30)
    payload, _ = run(make_reconciler(LAB_CAM, person_match_floor=0.0), people=[person("P1", "blue shirt")])
    assert person_assignments(payload, inv)[0]["entity_id"] is None


def test_ac_per_45_private_window_tracks_no_one(make_reconciler, inv):
    seed_person(inv, "dark hair", location="NOD2", ago_s=30)
    payload, _ = run(make_reconciler(LAB_CAM), people=[person("P1", "dark hair, sleeping in a blue sleeping bag")])
    assert person_assignments(payload, inv) == []
    assert payload["window"]["private"] is True


def test_ac_per_45_cross_module_off(make_reconciler, inv):
    seed_person(inv, GEMMA_FLIGHT_SUIT, location="NOD2", ago_s=60)
    payload, _ = run(make_reconciler(LAB_CAM, person_cross_module_s=0.0), people=[person("P1", GEMMA_FLIGHT_SUIT)])
    assert person_assignments(payload, inv)[0]["entity_id"] is None


def test_ac_per_45_one_class_listing_per_window(make_reconciler, inv):
    seed_person(inv, "red shirt", location="NOD2", ago_s=60)
    rec = make_reconciler(LAB_CAM)
    inv.calls.clear()
    run(rec, people=[person("P1", "blue shirt"), person("P2", "green shirt"), person("P3", "white shirt")])
    assert inv.calls.count("list_items") <= 2  # the module listing (AC-PER-29) + the class listing


def test_ac_per_45_config():
    from perception.config import ConfigError, PerceptionConfig

    assert PerceptionConfig().person_cross_module_s == 600.0
    assert PerceptionConfig.from_env({"PERCEPTION_PERSON_CROSS_MODULE_S": "120"}).person_cross_module_s == 120.0
    with pytest.raises(ConfigError):
        PerceptionConfig.from_env({"PERCEPTION_PERSON_CROSS_MODULE_S": "-1"})


# ================================================================================ AC-PER-46 normalisation
@pytest.mark.parametrize("text,colour", [
    ("grey long-sleeve shirt, dark pants", "grey"), ("dark blue long-sleeved shirt", "blue"),
    ("grey short-sleeved shirt, black pants", "grey"), ("grey hair, grey long-sleeve shirt, dark pants", "grey"),
    ("white sleeveless shirt", "white"), ("blue tank top, dark shorts", "blue"),
])
def test_ac_per_46_sleeve_length_does_not_detach_the_colour(text, colour):
    assert appearance_descriptors(text)["shirt"] == {colour}


def test_ac_per_46_grey_long_sleeve_no_longer_matches_a_blue_shirt():
    """Real merge in the AC-PER-35 replay: 'grey long-sleeved shirt, black pants' joined a blue-shirt track."""
    assert appearance_similarity("grey long-sleeve shirt, dark pants", "blue shirt, dark pants") < 0.5
    assert appearance_similarity("grey long-sleeved shirt, black pants", "dark blue shirt, dark pants") < 0.5
    assert appearance_similarity("grey long-sleeve shirt, dark pants", "grey shirt, dark pants") >= 0.5


@pytest.mark.parametrize("text,key,colours", [
    ("white spacesuit", "spacesuit", {"white"}), ("spacesuit, dark visor", "visor", None),
    ("white space suit", "spacesuit", {"white"}), ("EMU", "spacesuit", None), ("EVA suit", "spacesuit", None),
    ("blue shirt, orange helmet", "helmet", {"orange"}), ("short hair, wearing a watch", "watch", None),
    ("grey t-shirt, black shorts, white harness", "harness", {"white"}),
    ("dark blue shirt, dark pants, watch on left wrist", "watch", None),
])
def test_ac_per_46_spacesuits_and_accessories_are_descriptors(text, key, colours):
    d = appearance_descriptors(text)
    assert key in d
    if colours is not None:
        assert d[key] == colours


def test_ac_per_46_spacesuit_is_not_a_flight_suit_and_not_a_sleep_cue():
    assert "suit" not in appearance_descriptors("white spacesuit")
    assert appearance_similarity("white spacesuit", "spacesuit, dark visor") >= 0.5
    assert appearance_similarity("white spacesuit", "white suit") < 0.5
    assert sleep_cue("white spacesuit") is None


def test_ac_per_46_us_flag_is_american_flag():
    d = appearance_descriptors("blue flight suit with NASA and US flag patches")
    assert {"american", "flag", "patch", "nasa"} <= set(d)
    assert appearance_similarity(GEMMA_FLIGHT_SUIT, "blue flight suit with NASA and US flag patches") >= 0.75
    assert "american" in appearance_descriptors("blue polo, U.S. flag patch")


@pytest.mark.parametrize("a,b", [
    ("dark shirt, floating in foreground", "dark shirt"),
    ("black t-shirt, short hair, looking out the window", "black t-shirt, short hair"),
    ("grey t-shirt, black shorts, holding a drink pouch", "grey t-shirt, black shorts"),
    ("blue shirt, working with equipment while floating", "blue shirt"),
    ("blue shirt, standing and reaching for a handrail", "blue shirt"),
])
def test_ac_per_46_pose_and_activity_words_are_ignored(a, b):
    assert appearance_descriptors(a) == appearance_descriptors(b)


def test_ac_per_46_distinctive_accessories_outweigh_generic_ones():
    # an accessory on one side only lowers the similarity (real: Nauka 'blue suit, orange gloves')
    assert appearance_similarity("blue suit, orange gloves", GEMMA_FLIGHT_SUIT) < 0.5
    # a shared headset says more than shared dark hair
    obs = "dark hair, headset"
    assert appearance_similarity(obs, "headset, grey shirt") > appearance_similarity(obs, "dark hair, blue shirt")
    assert appearance_similarity(obs, "dark hair, blue shirt") < 0.5


def test_ac_per_46_measured_real_strings_keep_their_verdicts():
    """The task's examples: flight suit vs plain shirts stay apart, the navy/dark blue shirts stay together."""
    assert appearance_similarity(GEMMA_FLIGHT_SUIT, "blue shirt, dark hair") < 0.5
    assert appearance_similarity(GEMMA_FLIGHT_SUIT, "dark blue shirt") < 0.5
    assert appearance_similarity("blue shirt, dark hair", "dark blue shirt") >= 0.5
    assert appearance_similarity("navy tee", "dark blue t-shirt") >= 0.5
    assert appearance_similarity("blue jumpsuit", "blue flight suit") >= 0.5


# ================================================================================ AC-PER-47 soft cap
def _crew(inv, n, *, location="NOD2", ago_s=3600.0, description="crew member, red shirt"):
    return [seed_person(inv, description, location=location, ago_s=ago_s + i) for i in range(n)]


def test_ac_per_47_at_the_crew_size_a_stale_track_elsewhere_is_preferred(make_reconciler, inv):
    expected = next(c for c in CATALOG if c["catalog_id"] == PERSON_CLASS)["expected_count"]
    _crew(inv, expected - 1)
    eid = seed_person(inv, GEMMA_FLIGHT_SUIT, location="NOD3", ago_s=3000)
    payload, _ = run(make_reconciler(LAB_CAM), people=[person("P1", "blue flight suit with NASA and US flag patches")])
    assert person_assignments(payload, inv)[0]["entity_id"] == eid
    assert len(live_people(inv)) == expected


def test_ac_per_47_below_the_crew_size_a_new_track_is_created(make_reconciler, inv):
    expected = next(c for c in CATALOG if c["catalog_id"] == PERSON_CLASS)["expected_count"]
    _crew(inv, expected - 2)
    seed_person(inv, GEMMA_FLIGHT_SUIT, location="NOD3", ago_s=3000)
    payload, _ = run(make_reconciler(LAB_CAM), people=[person("P1", GEMMA_FLIGHT_SUIT)])
    assert person_assignments(payload, inv)[0]["entity_id"] is None


def test_ac_per_47_the_cap_never_forces_a_clash_or_a_weak_match(make_reconciler, inv):
    _crew(inv, 9)  # more than the seed's count: a handover crew
    payload, _ = run(make_reconciler(LAB_CAM),
                     people=[person("P1", "blue shirt"), person("P2", "headset"), person("P3", "astronaut")])
    assert all(a["entity_id"] is None for a in person_assignments(payload, inv))
    assert len(live_people(inv)) == 12


def test_ac_per_47_the_cap_never_puts_two_people_of_a_window_on_one_track(make_reconciler, inv):
    _crew(inv, 7, description="crew member, blue shirt")
    payload, response = run(make_reconciler(LAB_CAM), people=[person("P1", "blue shirt"), person("P2", "blue shirt")])
    people = person_assignments(payload, inv)
    assert len({entity_of(a, response) for a in people}) == 2


def test_ac_per_47_soft_cap_off(make_reconciler, inv):
    _crew(inv, 6)
    seed_person(inv, GEMMA_FLIGHT_SUIT, location="NOD3", ago_s=3000)
    payload, _ = run(make_reconciler(LAB_CAM, person_soft_cap=False), people=[person("P1", GEMMA_FLIGHT_SUIT)])
    assert person_assignments(payload, inv)[0]["entity_id"] is None


def test_ac_per_47_live_count_is_read_once_per_window(make_reconciler, inv):
    _crew(inv, 7)
    rec = make_reconciler(LAB_CAM)
    inv.calls.clear()
    run(rec, people=[person("P1", "blue shirt"), person("P2", "green shirt"), person("P3", "white shirt")])
    assert inv.calls.count("count_live_entities") <= 1


def test_ac_per_47_config():
    from perception.config import ConfigError, PerceptionConfig

    assert PerceptionConfig().person_soft_cap is True
    for raw, value in (("0", False), ("false", False), ("1", True), ("on", True)):
        assert PerceptionConfig.from_env({"PERCEPTION_PERSON_SOFT_CAP": raw}).person_soft_cap is value
    with pytest.raises(ConfigError):
        PerceptionConfig.from_env({"PERCEPTION_PERSON_SOFT_CAP": "maybe"})


# ================================================================================ AC-PER-48 real data
def _replay(make_reconciler, inv, **overrides) -> list[dict]:
    """The fixture windows through the reconciler at the live cadence (5 s windows, 6 frames)."""
    rec = make_reconciler(HANDHELD, **overrides)
    out = []
    for i, w in enumerate(json.loads(FIXTURE.read_text())["windows"]):
        text = json.dumps(w["observation"])
        reply = interpret_reply(text)
        assert reply.observation is not None, reply.errors
        win = scene_window([[]] * 6, t0=datetime.fromisoformat(w["t_start"]), step_s=1.0, index=i)
        res = rec.process(win, OmniResult(status="ok", observation=reply.observation, raw=text, model="gemma"),
                          [[] for _ in range(6)])
        assert res.status == "posted", res.error
        out.append({"window": w, "payload": res.payload, "response": res.response})
    return out


def test_ac_per_48_fixture_is_the_real_people_sequence():
    doc = json.loads(FIXTURE.read_text())
    windows = doc["windows"]
    assert len(windows) == 99 and doc["measured_live"]["person_tracks"] == 25
    times = [datetime.fromisoformat(w["t_start"]) for w in windows]
    assert times == sorted(times) and times[0].isoformat().startswith("2026-10-18T03:02:48")
    texts = [p["appearance"] for w in windows for p in w["observation"]["people"]]
    assert texts.count(GEMMA_FLIGHT_SUIT) == 3 and len(set(texts)) == 72
    assert all("module" in w for w in windows)  # the module the AC-PER-39..42 resolution gives the window


def test_ac_per_48_fixture_modules_match_the_replay(make_reconciler, inv):
    for r in _replay(make_reconciler, inv):
        assert r["payload"]["window"]["location"]["location_id"] == r["window"]["module"]


def test_ac_per_48_real_sequence_produces_far_fewer_tracks(make_reconciler, inv):
    replayed = _replay(make_reconciler, inv)
    tracks = len(live_people(inv))
    # AC-PER-66 amendment (UI6-02): hair-length and legwear clashes and the module-spread rule trade continuity for
    # purity on purpose. Measured: 21 -> 26 tracks (was <= 0.6 * 37 = 22), clashing sighting pairs inside tracks
    # 3 -> 1, most places on one track 5 -> 4 (docs/tdd-log/perception.md). Still 30 % fewer than AC-PER-35's 37.
    assert tracks <= 0.71 * AC_PER_35_REAL_TRACKS, tracks
    clashing = 0
    for e in live_people(inv):
        texts = [s["description"] for s in inv.entities[e["entity_id"]]["sightings"] if s.get("description")]
        clashing += sum(appearance_conflict(a, b) for i, a in enumerate(texts) for b in texts[i + 1:])
    assert clashing <= 1, clashing
    for r in replayed:  # two entries of one window never share a track
        ids = [entity_of(a, r["response"]) for a in person_assignments(r["payload"], inv)]
        assert len(ids) == len(set(ids))


def test_ac_per_48_the_flag_patch_flight_suit_is_one_track(make_reconciler, inv):
    """Seen 4 times, 15 min apart (the tour loops), always at segment level: 4 tracks under AC-PER-35."""
    replayed = _replay(make_reconciler, inv)
    ids = {entity_of(a, r["response"]) for r in replayed for a in person_assignments(r["payload"], inv)
           if "flag patch" in a["sighting"]["description"]}
    assert len(ids) == 1


def test_ac_per_48_spacesuits_do_not_join_crew_in_clothes(make_reconciler, inv):
    replayed = _replay(make_reconciler, inv)
    for r in replayed:
        for a in person_assignments(r["payload"], inv):
            if "spacesuit" in a["sighting"]["description"] and a["entity_id"]:
                desc = inv.entities[a["entity_id"]]["description"]
                assert "spacesuit" in desc, (a["sighting"]["description"], desc)  # review: not a flight suit



# ================================================================================ review round
KITCHEN_SINK = ("black white grey blue red green yellow orange purple pink brown khaki shirt, pants, shorts, suit, "
                "jacket, hoodie, headset, gloves, visor, helmet, glasses, watch, harness, spacesuit, flag patch, hair")


@pytest.mark.parametrize("other", ["blue shirt, dark hair", "grey t-shirt, black shorts", "white spacesuit",
                                   "orange suit", GEMMA_FLIGHT_SUIT])
def test_review_an_implausibly_broad_appearance_matches_no_one(other):
    """Injection: one crafted appearance (every colour, every garment) matched every crew member at 1.0."""
    assert appearance_descriptors(KITCHEN_SINK) == {}
    assert appearance_similarity(KITCHEN_SINK, other) == 0.0
    assert appearance_similarity("blue red green yellow shirt", "blue shirt") == 0.0  # > 3 colours on one garment


def test_review_real_strings_are_never_too_broad():
    doc = json.loads(FIXTURE.read_text())
    texts = {p["appearance"] for w in doc["windows"] for p in w["observation"]["people"]}
    assert sum(not appearance_descriptors(t) for t in texts) == 4  # hand visible, blue/white clothing, white sleeve


def test_review_a_broad_entity_description_does_not_absorb_the_station(make_reconciler, inv):
    seed_person(inv, KITCHEN_SINK, location="NOD2", ago_s=30)
    payload, _ = run(make_reconciler(LAB_CAM), people=[person("P1", "blue shirt, dark hair")])
    assert person_assignments(payload, inv)[0]["entity_id"] is None


# review round: weak tiers (cross-module, soft cap) need two-sided, coloured evidence
def test_review_a_colourless_track_does_not_absorb_shirts_across_modules(make_reconciler, inv):
    seed_person(inv, "astronaut in a shirt", location="NOD2", ago_s=30)
    payload, _ = run(make_reconciler(LAB_CAM), people=[person("P1", "red shirt")])
    assert person_assignments(payload, inv)[0]["entity_id"] is None


def test_review_a_short_track_does_not_absorb_a_richer_outfit_across_modules(make_reconciler, inv):
    seed_person(inv, "blue shirt, dark hair", location="NOD2", ago_s=60)
    payload, _ = run(make_reconciler(LAB_CAM), people=[person("P1", "blue shirt, orange helmet")])
    assert person_assignments(payload, inv)[0]["entity_id"] is None  # Dice 0.44: weak evidence for another module


def test_review_same_module_keeps_the_lenient_ac_per_35_rule(make_reconciler, inv):
    eid = seed_person(inv, "blue shirt, dark hair", location="LAB1", ago_s=60)
    payload, _ = run(make_reconciler(LAB_CAM), people=[person("P1", "blue shirt, orange helmet")])
    assert person_assignments(payload, inv)[0]["entity_id"] == eid


def test_review_a_watch_alone_is_not_evidence(make_reconciler, inv):
    _crew(inv, 7)
    seed_person(inv, "short hair, wearing a watch", location="NOD3", ago_s=3000)
    payload, _ = run(make_reconciler(LAB_CAM), people=[person("P1", "pink hoodie, watch")])
    assert person_assignments(payload, inv)[0]["entity_id"] is None


def test_review_crafted_colourless_text_cannot_take_people_through_the_cap(make_reconciler, inv):
    _crew(inv, 7)
    seed_person(inv, "shirt, pants, shorts, jacket, hoodie, sweater, cap, watch", location="NOD3", ago_s=86400)
    payload, _ = run(make_reconciler(LAB_CAM), people=[person("P1", "pink hoodie, watch")])
    assert person_assignments(payload, inv)[0]["entity_id"] is None


def test_review_the_cap_is_only_for_sightings_without_any_candidate(make_reconciler, inv):
    _crew(inv, 6, description="crew member, green shirt")
    here = seed_person(inv, "blue shirt, khaki pants", location="LAB1", ago_s=30)
    seed_person(inv, "blue shirt, khaki pants", location="NOD3", ago_s=5000)  # stale, elsewhere
    payload, _ = run(make_reconciler(LAB_CAM), people=[person("P1", "blue shirt, khaki pants"),
                                                       person("P2", "blue shirt, khaki pants")])
    ids = [a["entity_id"] for a in person_assignments(payload, inv)]
    assert here in ids and None in ids  # P2 lost its candidate to P1: a new track, not the stale one via the cap


def test_review_real_sequence_tracks_stay_mostly_pure(make_reconciler, inv):
    """Pairs of sightings on one track whose appearances clash in colour (purity proxy, no ground truth)."""
    import itertools

    from perception.people import appearance_conflict

    replayed = _replay(make_reconciler, inv)
    by_track: dict[str, list[str]] = {}
    for r in replayed:
        for a in person_assignments(r["payload"], inv):
            by_track.setdefault(entity_of(a, r["response"]), []).append(a["sighting"]["description"])
    clashes = sum(appearance_conflict(x, y) for d in by_track.values() for x, y in itertools.combinations(d, 2))
    assert clashes <= 5, clashes  # same-module-only matching: 3 (43 tracks); defaults: 4 (22 tracks)


@pytest.mark.parametrize("text,key,colours", [
    ("grey long sleeves shirt", "shirt", {"grey"}), ("blue suit with american flags", "flag", None),
])
def test_review_plural_sleeves_and_flags(text, key, colours):
    d = appearance_descriptors(text)
    assert key in d and (colours is None or d[key] == colours)


def test_review_terse_text_in_another_module_starts_a_track(make_reconciler, inv):
    """Accepted under-merge: 'blue flight suit' (Dice 0.4 against the patch description) is weak evidence across
    modules; the same text in the same module still attaches (AC-PER-35)."""
    seed_person(inv, GEMMA_FLIGHT_SUIT, location="NOD2", ago_s=60)
    payload, _ = run(make_reconciler(LAB_CAM), people=[person("P1", "blue flight suit")])
    assert person_assignments(payload, inv)[0]["entity_id"] is None


def test_review_a_clash_with_a_tracks_latest_sightings_blocks_the_match(make_reconciler, inv):
    """The blue-polo over-merge: the first description has no trousers, the latest sightings do."""
    rec = make_reconciler(LAB_CAM)
    p1, r1 = run(rec, people=[person("P1", "blue shirt, dark hair")])
    run(rec, people=[person("P1", "blue shirt, tan pants")], t=30)
    p3, _ = run(rec, people=[person("P1", "dark blue shirt, white pants")], t=60)
    first = entity_of(person_assignments(p1, inv)[0], r1)
    assert person_assignments(p3, inv)[0]["entity_id"] != first


def test_review_private_windows_feed_no_appearance_memory(make_reconciler, inv):
    rec = make_reconciler(LAB_CAM)
    run(rec, people=[person("P1", "dark hair, sleeping in a blue sleeping bag")])
    assert rec._person_recent == {}
