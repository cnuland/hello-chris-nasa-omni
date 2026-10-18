"""AC-PER-66 (UI6-02) same-class re-identification needs consistent colour and a known module; AC-PER-71 (UI6-10)
private windows attach by class and place.

Live (docs/ui-review/round6, database read-only): m-bag 7a4c756d went "another white m-bag on the floor" -> "white
storage bag" -> "large grey fabric bag" -> "blue storage bag attached to wall" (all USOS); person 1e8251b6 "blue shirt,
dark hair" took JPM1 07:49:36, NOD1 07:51:36, ZVEZDA 07:55:36, NAUKA 07:57:36 ...; laptop 8b4c0819 (private LAB1)
absorbed public USOS laptops; NOD2 gained 5 new CTBs and a laptop in the second sleep-cue window."""
from __future__ import annotations

from datetime import timedelta

import pytest
from support import T0, blend, detections_for, obj, observation, random_unit, scene_window
from test_ac_per_08_reconciler import BOX_A, BOX_B, vec

from perception import NEW
from perception.config import ConfigError, PerceptionConfig
from perception.fakes import FakeDecision
from perception.people import appearance_conflict, object_colour_conflict, object_colours
from perception.types import DecisionResult, OmniResult

HANDHELD = "cam-handheld"
BAG_FRAMES = [[("bag-A", BOX_A)]]


def process(rec, objects, *, hint=None, t=0.0, frames=None, labels=None, people=(), summary="crew in the module"):
    frames = frames if frames is not None else [[]]
    w = scene_window(frames, t0=T0 + timedelta(seconds=t))
    omni = OmniResult(status="ok", observation=observation(objects, people=people, location_hint=hint,
                                                           scene_summary=summary), model="fake-omni", raw="{}")
    out = rec.process(w, omni, detections_for(frames, labels or {}))
    assert out.status == "posted", out.error
    return out.payload


def const(p):
    return lambda features: p


def person(ref, appearance, activity="floating"):
    return {"ref": ref, "appearance": appearance, "activity": activity, "is_speaking": False}


# ================================================================================ colour helpers (real strings)
@pytest.mark.parametrize(("label", "description", "colours"), [
    ("m-bag", "another white m-bag on the floor", {"white"}),
    ("storage bag", "large grey fabric bag", {"grey"}),
    ("m-bag", "blue storage bag attached to wall", {"blue"}),
    ("m-bag", "large blue storage bag hanging on the right", {"blue"}),
    ("bag", "green fabric bag", {"green"}),
    ("laptop", "a small silver laptop", {"grey"}),
    ("white m-bag", "attached to a rack next to a blue laptop", {"white"}),
    ("m-bag", "bag next to a blue laptop", None),
    ("cargo transfer bag", "large white wrapped cargo container", {"white"}),
])
def test_object_colours(label, description, colours):
    got = object_colours(label, description)
    assert (set(got) if got is not None else None) == colours


def test_object_colour_conflict_on_the_live_chain():
    white = ("m-bag", "white storage bag")
    assert object_colour_conflict(white, ("m-bag", "blue storage bag attached to wall"))
    # review round: white and grey are one colour under station lighting (no split on "grey fabric bag")
    assert not object_colour_conflict(white, ("storage bag", "large grey fabric bag"))
    assert object_colour_conflict(white, ("bag", "green fabric bag"))
    assert not object_colour_conflict(white, ("m-bag", "a large white storage bag"))
    assert not object_colour_conflict(white, ("m-bag", "storage bag on the floor"))  # no colour: no evidence
    assert not object_colour_conflict(("m-bag", "dark bag"), ("m-bag", "blue storage bag"))  # dark family


# ================================================================================ object colour gate
def test_live_case_blue_bag_does_not_attach_to_the_white_bag(make_reconciler, inv):
    eid = inv.add_entity(catalog_id="m-bag", last_location="USOS", last_seen_at=T0 - timedelta(seconds=30),
                         description="another white m-bag on the floor")
    p = process(make_reconciler(HANDHELD), [obj("m-bag", None, description="blue storage bag attached to wall")])
    assert p["assignments"][0]["entity_id"] is None
    p = process(make_reconciler(HANDHELD), [obj("m-bag", None, description="a large white storage bag")], t=30)
    assert str(p["assignments"][0]["entity_id"]) == eid


def test_latest_sighting_descriptions_are_remembered(make_reconciler, inv):
    eid = inv.add_entity(catalog_id="m-bag", last_location="USOS", last_seen_at=T0 - timedelta(seconds=30),
                         description="m-bag")
    rec = make_reconciler(HANDHELD)
    p = process(rec, [obj("m-bag", None, description="white storage bag")])
    assert str(p["assignments"][0]["entity_id"]) == eid
    p = process(rec, [obj("m-bag", None, description="green fabric bag")], t=30)
    assert p["assignments"][0]["entity_id"] is None


def test_colour_clash_blocks_the_embedding_path_even_in_the_auto_band(make_reconciler, inv):
    eid = inv.add_entity(catalog_id="m-bag", embedding=vec("bag-A"), last_location="JPM1",
                         last_seen_at=T0 - timedelta(minutes=2), description="white m-bag attached to a rack")
    p = process(make_reconciler(HANDHELD, scorer=const(0.95)), [obj("m-bag", BOX_A, description="blue bag in the "
                                                                    "foreground right")], hint="Kibo",
                frames=BAG_FRAMES, labels={"bag-A": "m-bag"})
    assert p["assignments"][0]["entity_id"] is None
    p = process(make_reconciler(HANDHELD, scorer=const(0.95)), [obj("m-bag", BOX_A, description="white m-bag")],
                hint="Kibo", frames=BAG_FRAMES, labels={"bag-A": "m-bag"}, t=30)
    assert str(p["assignments"][0]["entity_id"]) == eid


def test_colour_clash_blocks_the_revisit_decision(make_reconciler, inv):
    a = inv.add_entity(catalog_id="drill-pistol", last_location="LAB1", last_seen_at=T0 - timedelta(hours=2),
                       description="black drill")
    inv.add_entity(catalog_id="drill-pistol", last_location="LAB1", last_seen_at=T0 - timedelta(hours=3),
                   description="orange drill")
    p = process(make_reconciler(HANDHELD), [obj("drill", None, description="a black pistol-grip drill")],
                hint="Destiny")
    assert str(p["assignments"][0]["entity_id"]) == a  # one candidate left after the gate: attached directly


# ================================================================================ module unknown (AC-PER-60)
def test_live_case_generic_class_segment_sighting_needs_the_auto_band(make_reconciler, inv):
    eid = inv.add_entity(catalog_id="m-bag", embedding=blend(vec("bag-A"), random_unit(3), 0.7), last_location="JPM1",
                         last_seen_at=T0 - timedelta(minutes=3), description="white storage bag")
    dec = FakeDecision(lambda q, o, s: DecisionResult(choice=eid, probabilities={eid: 0.9, NEW: 0.1}, model="d"))
    p = process(make_reconciler(HANDHELD, decision=dec, scorer=const(0.7)),
                [obj("m-bag", BOX_A, description="white storage bag attached to a rack")],
                frames=BAG_FRAMES, labels={"bag-A": "m-bag"})  # USOS: module unknown
    a = p["assignments"][0]
    assert a["entity_id"] is None and dec.calls == []
    assert [r["kind"] for r in p["reviews"]] == ["possible_duplicate"]


def test_segment_on_both_sides_is_module_unknown_too(make_reconciler, inv):
    inv.add_entity(catalog_id="laptop", embedding=blend(vec("laptop-A"), random_unit(3), 0.7),
                   last_location="USOS", last_seen_at=T0 - timedelta(minutes=8))
    p = process(make_reconciler(HANDHELD, scorer=const(0.7)), [obj("laptop", BOX_A)],
                frames=[[("laptop-A", BOX_A)]], labels={"laptop-A": "laptop"})
    assert p["assignments"][0]["entity_id"] is None


def test_same_known_module_keeps_the_ask_band(make_reconciler, inv):
    eid = inv.add_entity(catalog_id="laptop", embedding=blend(vec("laptop-A"), random_unit(3), 0.7),
                         last_location="JPM1F5", last_seen_at=T0 - timedelta(minutes=8))
    dec = FakeDecision(lambda q, o, s: DecisionResult(choice=eid, probabilities={eid: 0.9, NEW: 0.1}, model="d"))
    p = process(make_reconciler(HANDHELD, decision=dec, scorer=const(0.7)), [obj("laptop", BOX_A)], hint="Kibo",
                frames=[[("laptop-A", BOX_A)]], labels={"laptop-A": "laptop"})
    assert str(p["assignments"][0]["entity_id"]) == eid and len(dec.calls) == 1


# ================================================================================ person module spread
LIVE_TRACK = [  # (minutes after 07:49:36, hint, appearance) of person 1e8251b6
    (0.0, "Kibo", "blue shirt, dark hair"),
    (2.0, "Node 1", "dark hair, blue shirt"),
    (6.0, "Zvezda", "wearing a blue shirt and shorts"),
    (8.0, "Nauka", "dark blue shirt, short hair"),
]


def test_live_case_blue_shirt_track_does_not_reach_a_third_module_on_weak_evidence(make_reconciler, inv):
    rec = make_reconciler(HANDHELD)
    ids = []
    for minutes, hint, appearance in LIVE_TRACK:
        p = process(rec, [], people=[person("P1", appearance)], hint=hint, t=minutes * 60)
        a = p["assignments"][0]
        ids.append(str(a["entity_id"]) if a["entity_id"] else None)
    assert ids[0] is None and ids[1] is not None  # JPM1 -> NOD1: the second module is still allowed
    assert ids[2] is None  # ZVEZDA would be the third module within 10 min on a 0.67 similarity: a new track


def test_third_module_with_strong_evidence_attaches(make_reconciler, inv):
    rec = make_reconciler(HANDHELD)
    looks = "blue flight suit with American flag patch, orange gloves"
    for i, hint in enumerate(["Kibo", "Node 1", "Zvezda"]):
        p = process(rec, [], people=[person("P1", looks)], hint=hint, t=i * 120)
    assert p["assignments"][0]["entity_id"] is not None


def test_spread_window_expires(make_reconciler, inv):
    rec = make_reconciler(HANDHELD)
    for t, hint, appearance in [(0, "Kibo", "blue shirt, dark hair"), (120, "Node 1", "dark hair, blue shirt"),
                                (700, "Zvezda", "blue shirt, khaki pants, dark hair")]:
        p = process(rec, [], people=[person("P1", appearance)], hint=hint, t=t)
    assert p["assignments"][0]["entity_id"] is not None  # JPM1 sighting is > 600 s old: ZVEZDA is the second


def test_spread_off(make_reconciler, inv):
    rec = make_reconciler(HANDHELD, person_spread_s=0)
    ids = []
    for minutes, hint, appearance in LIVE_TRACK[:3]:
        p = process(rec, [], people=[person("P1", appearance)], hint=hint, t=minutes * 60)
        ids.append(p["assignments"][0]["entity_id"])
    assert ids[2] is not None


def test_segment_sightings_add_no_module(make_reconciler, inv):
    rec = make_reconciler(HANDHELD)
    for t, hint in [(0, "Kibo"), (60, None), (120, None), (180, "Node 1")]:
        p = process(rec, [], people=[person("P1", "blue shirt, dark hair")], hint=hint, t=t)
    assert p["assignments"][0]["entity_id"] is not None


def test_a_module_the_track_was_just_in_is_not_new(make_reconciler, inv):
    """Eval crew transit: NOD1 -> COL1 -> JPM1, then more JPM1 sightings with a weaker text are not a third module."""
    rec = make_reconciler(HANDHELD)
    texts = ["crew member, green shirt", "green shirt, dark hair", "green shirt, dark hair", "green shirt, khaki pants"]
    ids = []
    for i, (hint, text) in enumerate(zip(["Node 1", "Columbus", "Kibo", "Kibo"], texts, strict=True)):
        p = process(rec, [], people=[person("P1", text)], hint=hint, t=i * 20)
        ids.append(p["assignments"][0]["entity_id"])
    assert ids[0] is None and all(ids[1:]) and len(set(ids[1:])) == 1


def test_hair_length_clashes():
    assert appearance_conflict("blue shirt, long hair", "dark blue shirt, short hair")
    assert appearance_conflict("long dark hair", "short-haired, blue shirt")
    assert not appearance_conflict("blue shirt, long hair", "blue shirt, dark hair")
    assert not appearance_conflict("grey long-sleeve shirt", "grey short sleeve shirt")


def test_legwear_type_clashes():
    """Live 1e8251b6: "blue t-shirt, tan pants" at 07:53 and "wearing a blue shirt and shorts" at 07:55."""
    assert appearance_conflict("blue t-shirt, tan pants", "wearing a blue shirt and shorts")
    assert not appearance_conflict("blue shirt, khaki pants", "blue shirt, trousers")
    assert not appearance_conflict("blue shirt, dark shorts", "blue shirt")


def test_spread_config_from_env():
    cfg = PerceptionConfig.from_env({"PERCEPTION_PERSON_SPREAD_S": "300", "PERCEPTION_PERSON_SPREAD_MODULES": "3",
                                     "PERCEPTION_PERSON_SPREAD_FLOOR": "0.9"})
    assert (cfg.person_spread_s, cfg.person_spread_modules, cfg.person_spread_floor) == (300, 3, 0.9)
    d = PerceptionConfig()
    assert (d.person_spread_s, d.person_spread_modules, d.person_spread_floor) == (600, 2, 0.8)
    with pytest.raises(ConfigError):
        PerceptionConfig.from_env({"PERCEPTION_PERSON_SPREAD_FLOOR": "2"})


# ================================================================================ private-only entities
def _private_laptop(inv, location="LAB1"):
    eid = inv.add_entity(catalog_id="laptop", embedding=vec("laptop-A"), last_location=location,
                         last_seen_at=T0 - timedelta(minutes=1), description=None)
    inv.entities[eid]["snapshot_withheld_reason"] = "private_window"
    return eid


def test_live_case_public_segment_laptop_does_not_join_a_private_only_laptop(make_reconciler, inv):
    _private_laptop(inv, location="USOS")
    p = process(make_reconciler(HANDHELD, scorer=const(0.95)), [obj("laptop", BOX_A,
                                                                    description="a small silver laptop")],
                frames=[[("laptop-A", BOX_A)]], labels={"laptop-A": "laptop"})
    assert p["assignments"][0]["entity_id"] is None


def test_public_laptop_joins_a_private_only_laptop_in_the_same_module_in_the_auto_band(make_reconciler, inv):
    eid = _private_laptop(inv)
    p = process(make_reconciler(HANDHELD, scorer=const(0.95)), [obj("laptop", BOX_A)], hint="Destiny",
                frames=[[("laptop-A", BOX_A)]], labels={"laptop-A": "laptop"})
    assert str(p["assignments"][0]["entity_id"]) == eid


def test_public_laptop_without_exemplar_joins_a_private_only_one_only_in_the_same_module(make_reconciler, inv):
    _private_laptop(inv, location="USOS")
    p = process(make_reconciler(HANDHELD), [obj("laptop", None, description="grey laptop mounted on a rack")])
    assert p["assignments"][0]["entity_id"] is None
    eid = _private_laptop(inv, location="LAB1")
    p = process(make_reconciler(HANDHELD), [obj("laptop", None)], hint="Destiny", t=30)
    assert str(p["assignments"][0]["entity_id"]) == eid


# ================================================================================ AC-PER-71 private windows
SLEEP = "a crew member sleeping in a sleeping bag"
NOD2_FRAMES = [[("ctb-1", (0.05, 0.1, 0.2, 0.3)), ("ctb-2", (0.25, 0.1, 0.4, 0.3)), ("ctb-3", (0.45, 0.1, 0.6, 0.3)),
                ("ctb-4", (0.65, 0.1, 0.8, 0.3)), ("ctb-5", (0.05, 0.5, 0.2, 0.7))]]
NOD2_LABELS = {f"ctb-{i}": "cargo transfer bag" for i in range(1, 6)}


def nod2_ctbs():
    return [obj("cargo transfer bag", box, description="white bag") for _, box in NOD2_FRAMES[0]]


def test_live_case_second_private_view_of_nod2_adds_nothing(make_reconciler, inv):
    rec = make_reconciler(HANDHELD)
    p1 = process(rec, nod2_ctbs(), hint="Harmony", summary=SLEEP, frames=NOD2_FRAMES, labels=NOD2_LABELS)
    assert p1["window"]["private"] and sum(1 for a in p1["assignments"] if a["entity_id"] is None) == 5
    p2 = process(rec, nod2_ctbs(), hint="Harmony", summary=SLEEP, frames=NOD2_FRAMES, labels=NOD2_LABELS, t=900)
    assert all(a["entity_id"] for a in p2["assignments"]) and len({a["entity_id"] for a in p2["assignments"]}) == 5
    assert all(a["exemplar"] is None for a in p1["assignments"] + p2["assignments"])
    assert p2["reviews"] == [] and p2["decisions"] == []


def test_private_window_creates_only_the_difference(make_reconciler, inv):
    existing = inv.add_entity(catalog_id="ctb-half", last_location="NOD2", last_seen_at=T0 - timedelta(minutes=15))
    p = process(make_reconciler(HANDHELD), nod2_ctbs(), hint="Harmony", summary=SLEEP, frames=NOD2_FRAMES,
                labels=NOD2_LABELS)
    attached = [str(a["entity_id"]) for a in p["assignments"] if a["entity_id"]]
    assert attached == [existing] and sum(1 for a in p["assignments"] if a["entity_id"] is None) == 4


def test_private_window_never_attaches_across_modules_or_classes(make_reconciler, inv):
    inv.add_entity(catalog_id="ctb-half", last_location="LAB1", last_seen_at=T0 - timedelta(minutes=1))
    inv.add_entity(catalog_id="m-bag", last_location="NOD2", last_seen_at=T0 - timedelta(minutes=1))
    p = process(make_reconciler(HANDHELD), nod2_ctbs()[:1], hint="Harmony", summary=SLEEP, frames=[NOD2_FRAMES[0][:1]],
                labels=NOD2_LABELS)
    assert p["assignments"][0]["entity_id"] is None


def test_private_window_does_not_embed(make_reconciler, inv, embedder):
    calls = []
    original = embedder.embed
    embedder.embed = lambda crop: calls.append(1) or original(crop)
    process(make_reconciler(HANDHELD), nod2_ctbs()[:2], hint="Harmony", summary=SLEEP, frames=[NOD2_FRAMES[0][:2]],
            labels=NOD2_LABELS)
    assert calls == [] and "candidates" not in inv.calls


def test_private_window_prefers_the_own_zone_then_the_most_recent(make_reconciler, inv):
    older = inv.add_entity(catalog_id="laptop", last_location="NOD2", last_seen_at=T0 - timedelta(hours=2))
    newer = inv.add_entity(catalog_id="laptop", last_location="NOD2", last_seen_at=T0 - timedelta(minutes=20))
    p = process(make_reconciler(HANDHELD), [obj("laptop", BOX_B)], hint="Harmony", summary=SLEEP,
                frames=[[("laptop-B", BOX_B)]], labels={"laptop-B": "laptop"})
    assert str(p["assignments"][0]["entity_id"]) == newer and older != newer
