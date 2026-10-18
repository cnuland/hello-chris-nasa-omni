"""Adversarial review of AC-PER-64..71 (UI review round 6): each finding reproduced first, then fixed."""
from __future__ import annotations

from datetime import timedelta

import numpy as np
import pytest
from support import CATALOG, T0, detections_for, obj, observation, scene_window
from test_ac_per_08_reconciler import BOX_A, vec

from perception.labels import CatalogMapper, is_worn_item
from perception.people import is_empty_suit, object_colour_conflict
from perception.types import OmniResult

HANDHELD = "cam-handheld"


def process(rec, objects, *, hint="Columbus", t=0.0, frames=None, labels=None, people=(), events=(), transcript=""):
    frames = frames if frames is not None else [[]]
    w = scene_window(frames, t0=T0 + timedelta(seconds=t))
    omni = OmniResult(status="ok", observation=observation(objects, people=people, events=events, location_hint=hint,
                                                           transcript=transcript), model="fake-omni", raw="{}")
    out = rec.process(w, omni, detections_for(frames, labels) if labels else [[] for _ in frames])
    assert out.status == "posted", out.error
    return out.payload


def person(ref, appearance, activity="floating", speaking=False):
    return {"ref": ref, "appearance": appearance, "activity": activity, "is_speaking": speaking}


# P1-1 remap through a verb
@pytest.mark.parametrize("description", ["crew member wearing a headset", "astronaut typing while wearing headphones",
                                         "black case containing a multimeter"])
def test_review_remap_never_crosses_a_verb_or_a_person(make_reconciler, inv, description):
    p = process(make_reconciler(HANDHELD), [obj("laptop", BOX_A, description=description)])
    assert p["assignments"][0]["new_entity"]["catalog_id"] == "laptop"


# P1-2 kind guard vs the seed's food/drink classes
@pytest.mark.parametrize(("label", "catalog_id"), [("coffee", "drink-pouch"), ("drink", "drink-pouch"),
                                                   ("meal", "food-pack"), ("food", "food-pack")])
def test_review_kind_guard_keeps_seed_fuzzy_matches(label, catalog_id):
    assert CatalogMapper(CATALOG).map(label).catalog_id == catalog_id


def test_review_kind_guard_never_rejects_a_seed_name_or_synonym():
    mapper = CatalogMapper(CATALOG)
    for c in CATALOG:
        for name in [c["class_name"], c["display_name"], *(c.get("synonyms") or [])]:
            assert mapper.map(name).catalog_id != "UNKNOWN", name


# P1-3 worn phrasing about someone else
@pytest.mark.parametrize(("label", "description"), [
    ("metal canister", "a metal canister floating beside a crew member wearing a blue shirt"),
    ("sticker", "a sticker on the face of the rack"),
    ("camera mount", "a camera mounted on the arm"),
])
def test_review_worn_phrase_about_a_nearby_person_is_not_worn(label, description):
    assert not is_worn_item(label, description, True)


# P1-4 converted suit text with "wearing"
def test_review_converted_suit_survives_the_worn_filter(make_reconciler, inv):
    p = process(make_reconciler(HANDHELD), [], people=[person("P1", "astronaut wearing white spacesuit",
                                                          "floating near the hatch")])
    [a] = p["assignments"]
    assert a["new_entity"]["label"] == "spacesuit" and p["window"]["worn_filtered"] == 0


# P2-5/6 structure memory place rules
def _mem(label, tokens, location, module=None):
    return {"memory_id": 1, "label": label, "label_key": label.replace(" ", ""), "description_tokens": tokens,
            "location_id": location, "module_id": module, "embedding": None}


def test_review_generic_label_memory_does_not_suppress_nearby(make_reconciler, inv):
    inv.structure_memories = [_mem("strap", ["strap", "white"], "USOS")]
    p = process(make_reconciler(HANDHELD), [obj("strap", BOX_A, description="white strap")], hint="Destiny")
    assert p["window"]["structure_memory_suppressed"] == 0 and p["assignments"]
    p = process(make_reconciler(HANDHELD), [obj("strap", BOX_A, description="white strap")], hint=None, t=30)
    assert p["window"]["structure_memory_suppressed"] == 1  # the same place: the operator's own words


# P2-7 colour gate when the candidate is outside the class listing
def test_review_colour_gate_uses_the_sighting_memory_without_a_listing_row(make_reconciler, inv):
    eid = inv.add_entity(catalog_id="m-bag", embedding=vec("bag-A"), last_location="JPM1",
                         last_seen_at=T0 - timedelta(minutes=2), description=None)
    rec = make_reconciler(HANDHELD, scorer=lambda f: 0.95)
    rec._object_recent[eid] = __import__("collections").deque(["white m-bag attached to a rack"])
    inv.list_items = lambda **kw: []  # the class listing misses the candidate
    p = process(rec, [obj("m-bag", BOX_A, description="blue bag in the foreground")], hint="Kibo",
                frames=[[("bag-A", BOX_A)]], labels={"bag-A": "m-bag"})
    assert p["assignments"][0]["entity_id"] is None


# P2-8 colour phrase cuts and lighting
@pytest.mark.parametrize(("a", "b"), [(("laptop", "laptop displaying a blue screen"), ("laptop", "silver laptop")),
                                      (("bag", "bag containing red tools"), ("bag", "white bag")),
                                      (("m-bag", "white storage bag"), ("storage bag", "large grey fabric bag"))])
def test_review_colour_false_clashes(a, b):
    assert not object_colour_conflict(a, b)


def test_review_blue_and_white_still_clash():
    assert object_colour_conflict(("m-bag", "white storage bag"), ("m-bag", "blue storage bag"))


# P2-9 refinements are moves to keep
def test_review_speech_refinement_from_segment_to_module_is_kept(make_reconciler, inv):
    inv.add_entity(catalog_id="drill-pistol", last_location="USOS", last_seen_at=T0 - timedelta(hours=1))
    p = process(make_reconciler(), [], hint=None, transcript="I'm putting the drill in node 2.")
    assert [(e["from_location"], e["to_location"]) for e in p["events"] if e["event_type"] == "moved"] == [
        ("USOS", "NOD2")]


# P2-10 converted person object keeps no crop
def test_review_converted_person_object_has_no_box_and_no_exemplar(make_reconciler, inv):
    box = (0.2, 0.2, 0.5, 0.8)
    p = process(make_reconciler(HANDHELD), [obj("person", box, description="astronaut in white spacesuit")],
                frames=[[("suit-A", box)]], labels={"suit-A": "person"})
    [a] = [a for a in p["assignments"] if a["new_entity"]["label"] == "spacesuit"]
    assert a["exemplar"] is None and a["sighting"]["bbox"] is None


# P3-12 embedding dimension mismatch
def test_review_memory_embedding_of_another_dimension_is_ignored(make_reconciler, inv, embedder):
    m = _mem("dark keypad", ["dark", "keypad"], "ZVEZDA", "ZVEZDA")
    m["embedding"] = [0.1] * 768
    inv.structure_memories = [m]
    embedder.embed = lambda crop: np.ones(512, dtype=np.float32) / np.sqrt(512)
    inv.candidates = lambda *a, **k: []  # the fake inventory itself insists on 768-d
    frames = [[("dev-A", BOX_A)]]
    omni = OmniResult(status="ok", observation=observation([obj("odd thing", BOX_A, description="x")],
                                                           location_hint="Zvezda"), model="fake-omni", raw="{}")
    out = make_reconciler(HANDHELD).process(scene_window(frames), omni, detections_for(frames, {"dev-A": "odd thing"}))
    assert out.payload is not None  # the payload was built (only the 768-d ingest contract rejects the exemplar)


# P3-13 truthy is_speaking
@pytest.mark.parametrize("speaking", [True, "true", 1, "yes"])
def test_review_truthy_speaking_is_a_person(speaking):
    assert not is_empty_suit("astronaut in white spacesuit", "floating", speaking)


# P3-11 inferred modules do not count toward the spread
def test_review_carried_or_revisit_modules_are_not_recorded_for_the_spread(make_reconciler, inv):
    rec = make_reconciler(HANDHELD)
    process(rec, [], people=[person("P1", "blue shirt, dark hair")], hint="Kibo")
    process(rec, [], people=[person("P1", "blue shirt, dark hair")], hint=None, t=20)  # carried JPM1
    eid = next(iter(rec._person_modules))
    assert [m for _, m in rec._person_modules[eid]] == ["JPM1"]
