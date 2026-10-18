"""AC-PER-9 fungible items -> stock_updates (count per zone = max over the window), never instances.
AC-PER-10 events: omni events -> inventory events for the matched entity, actor from people[ref].appearance,
evidence = frame at frame_index stored to S3; a spoken placement statement -> `moved` event with source `speech`."""
from __future__ import annotations

from datetime import timedelta

import pytest
from support import T0, obj, observation, parse_dt, scene_window, uri_key
from test_ac_per_08_reconciler import BOX_A, process

from perception.fakes import FakeEmbedder
from perception.types import OmniResult

VEC = FakeEmbedder()
FOOD = [(0.05 + 0.15 * i, 0.1, 0.15 + 0.15 * i, 0.3) for i in range(6)]


def food_frames(counts):
    return [[(f"food-{j}", FOOD[j]) for j in range(n)] for n in counts]


def food_objects(counts):
    return [obj("food package", FOOD[j], frame_index=i) for i, n in enumerate(counts) for j in range(n)]


# ---------------------------------------------------------------------------------------------- AC-PER-9
def test_ac_per_09_fungible_items_become_stock_update_with_max_count(make_reconciler, inv):
    counts = [3, 5, 2]
    labels = {f"food-{j}": "food package" for j in range(6)}
    _, w = process(make_reconciler(), food_frames(counts), food_objects(counts), labels)
    p = inv.last
    assert p["stock_updates"] == [{"catalog_id": "food-pack", "location_id": "LAB1", "est_count": 5,
                                   "seen_at": p["stock_updates"][0]["seen_at"]}]
    assert w.t_start <= parse_dt(p["stock_updates"][0]["seen_at"]) <= w.t_end


def test_ac_per_09_fungible_items_create_no_instances(make_reconciler, inv):
    counts = [2, 2]
    labels = {f"food-{j}": "food package" for j in range(6)}
    process(make_reconciler(), food_frames(counts), food_objects(counts), labels)
    p = inv.last
    assert all((a.get("new_entity") or {}).get("catalog_id") != "food-pack" for a in p["assignments"])
    assert p["assignments"] == []
    assert inv.entities == {}


def test_ac_per_09_mixed_fungible_and_instance_items(make_reconciler, inv):
    frames = [[("food-0", FOOD[0]), ("food-1", FOOD[1]), ("laptop-A", (0.5, 0.5, 0.8, 0.8))]]
    objs = [obj("food pouch", FOOD[0]), obj("food package", FOOD[1]), obj("laptop", (0.5, 0.5, 0.8, 0.8))]
    process(make_reconciler(), frames, objs, {"food-0": "food package", "food-1": "food package",
                                              "laptop-A": "laptop"})
    p = inv.last
    assert [s["est_count"] for s in p["stock_updates"] if s["catalog_id"] == "food-pack"] == [2]
    assert [a["new_entity"]["catalog_id"] for a in p["assignments"]] == ["laptop"]


def test_ac_per_09_one_stock_update_per_fungible_class(make_reconciler, inv):
    frames = [[("food-0", FOOD[0]), ("wipe-0", FOOD[3]), ("wipe-1", FOOD[4])]]
    objs = [obj("food package", FOOD[0]), obj("wipes", FOOD[3]), obj("wet wipes", FOOD[4])]
    process(make_reconciler(), frames, objs, {"food-0": "food package", "wipe-0": "wipes", "wipe-1": "wipes"})
    by_cat = {s["catalog_id"]: s["est_count"] for s in inv.last["stock_updates"]}
    assert by_cat == {"food-pack": 1, "wipes": 2}


def test_ac_per_09_stock_location_follows_window_location(make_reconciler, inv):
    process(make_reconciler(), food_frames([1]), food_objects([1]), {"food-0": "food package"},
            location_hint="NOD1D1")
    assert inv.last["stock_updates"][0]["location_id"] == "NOD1D1"


# ---------------------------------------------------------------------------------------------- AC-PER-10 visual events
MOVE_PEOPLE = [{"ref": "P1", "appearance": "crew member, blue shirt, headset", "activity": "carrying a drill"}]


def _moved_event(frame_index=2, actor_ref="P1", label="drill", to="NOD2"):
    return {"type": "moved", "object_label": label, "actor_ref": actor_ref, "to_location_hint": to,
            "frame_index": frame_index, "evidence": "crew carried the drill", "confidence": 0.8}


def test_ac_per_10_omni_event_maps_to_matched_entity_with_actor_and_evidence(make_reconciler, inv, storage):
    eid = inv.add_entity(catalog_id="drill-pistol", embedding=VEC.vector_for_object("drill-1"),
                         last_location="LAB1", last_seen_at=T0 - timedelta(hours=1))
    frames = [[("drill-1", BOX_A)]] * 3
    _, w = process(make_reconciler(camera_id="cam-nod2-aft", scorer=lambda f: 0.95), frames,
                   [obj("drill", BOX_A, frame_index=2, held_by="P1")], {"drill-1": "drill"},
                   people=MOVE_PEOPLE, events=[_moved_event()])
    p = inv.last
    moved = [e for e in p["events"] if e["event_type"] == "moved"]
    assert len(moved) == 1
    ev = moved[0]
    target = ev.get("entity_id") or next(a["entity_id"] for a in p["assignments"] if a.get("ref") == ev["entity_ref"])
    assert str(target) == eid
    assert "blue shirt" in ev["actor_desc"]
    assert parse_dt(ev["occurred_at"]) == w.frames[2].timestamp
    assert ev["to_location"] == "NOD2"
    assert ev["from_location"] == "LAB1"
    assert ev["source"] == "pipeline"
    assert 0.0 <= ev["confidence"] <= 1.0
    key = uri_key(ev["evidence_uri"])
    assert key.startswith("frames/cam-nod2-aft/2026-10-01/") and key.endswith(".jpg")
    assert storage.objects[key][:2] == b"\xff\xd8"


@pytest.mark.parametrize("etype", ["added", "removed", "used", "handed_over"])
def test_ac_per_10_other_event_types_pass_through(make_reconciler, inv, etype):
    frames = [[("drill-1", BOX_A)]]
    ev = {"type": etype, "object_label": "drill", "actor_ref": "P1", "frame_index": 0, "confidence": 0.7}
    process(make_reconciler(), frames, [obj("drill", BOX_A)], {"drill-1": "drill"}, people=MOVE_PEOPLE, events=[ev])
    assert [e["event_type"] for e in inv.last["events"]] == [etype]
    assert inv.last["events"][0]["entity_ref"] == inv.last["assignments"][0]["ref"]


def _known_drill(inv):
    """AC-PER-63 amendment: a moved event needs a previous place, so these event tests use a drill last seen in NOD1."""
    return inv.add_entity(catalog_id="drill-pistol", embedding=VEC.vector_for_object("drill-1"),
                          last_location="NOD1", last_seen_at=T0 - timedelta(hours=1))


def test_ac_per_10_event_with_unknown_actor_ref_has_no_actor_desc(make_reconciler, inv):
    _known_drill(inv)
    process(make_reconciler(scorer=lambda f: 0.95), [[("drill-1", BOX_A)]], [obj("drill", BOX_A)], {"drill-1": "drill"},
            people=MOVE_PEOPLE, events=[_moved_event(frame_index=0, actor_ref="P7", to=None)])
    ev = inv.last["events"][0]
    assert ev.get("actor_desc") in (None, "")


def test_ac_per_10_event_for_object_not_observed_or_known_is_dropped(make_reconciler, inv):
    process(make_reconciler(), [[("drill-1", BOX_A)]], [obj("drill", BOX_A)], {"drill-1": "drill"},
            events=[_moved_event(frame_index=0, label="astrobee robot", actor_ref=None, to=None)])
    assert inv.last.get("events", []) == []


def test_ac_per_10_event_frame_index_out_of_range_falls_back_to_window_frame(make_reconciler, inv, storage):
    _known_drill(inv)
    process(make_reconciler(scorer=lambda f: 0.95), [[("drill-1", BOX_A)]], [obj("drill", BOX_A)], {"drill-1": "drill"},
            events=[_moved_event(frame_index=9, to=None)])
    ev = inv.last["events"][0]
    assert ev["evidence_uri"] is None or uri_key(ev["evidence_uri"]) in storage.objects


def test_ac_per_10_event_to_location_hint_unknown_is_null(make_reconciler, inv):
    """Amended by AC-PER-51 (UI4-06): an unresolvable hint still never becomes a location, but the event now names
    the place the object was sighted in this window (LAB1, the camera's module) instead of no place at all."""
    _known_drill(inv)
    process(make_reconciler(scorer=lambda f: 0.95), [[("drill-1", BOX_A)]], [obj("drill", BOX_A)], {"drill-1": "drill"},
            events=[_moved_event(frame_index=0, to="somewhere over the rainbow")])
    assert inv.last["events"][0].get("to_location") == "LAB1"


# ---------------------------------------------------------------------------------------------- AC-PER-10 speech events
def test_ac_per_10_spoken_statement_yields_speech_moved_event(make_reconciler, inv):
    eid = inv.add_entity(catalog_id="drill-pistol", embedding=VEC.vector_for_object("drill-1"),
                         last_location="LAB1", last_seen_at=T0 - timedelta(hours=1))
    rec = make_reconciler()
    _, w = process(rec, [[]], [], {}, transcript="I'm putting the drill in node 2.")
    speech = [e for e in inv.last["events"] if e["source"] == "speech"]
    assert len(speech) == 1
    ev = speech[0]
    assert ev["event_type"] == "moved"
    assert str(ev["entity_id"]) == eid
    assert ev["to_location"] == "NOD2"
    assert ev["confidence"] <= 0.6
    assert w.t_start <= parse_dt(ev["occurred_at"]) <= w.t_end + timedelta(seconds=5)


def test_ac_per_10_speech_uses_transcript_argument_over_omni_transcript(make_reconciler, inv):
    inv.add_entity(catalog_id="drill-pistol", embedding=VEC.vector_for_object("drill-1"), last_location="LAB1",
                   last_seen_at=T0 - timedelta(hours=1))
    w_obs = observation([], transcript="nothing to report")
    rec = make_reconciler()
    rec.process(scene_window([[]]), OmniResult(status="ok", observation=w_obs, model="m"), [[]],
                transcript="putting the PGT in Harmony")
    speech = [e for e in inv.last["events"] if e["source"] == "speech"]
    assert len(speech) == 1 and speech[0]["to_location"] == "NOD2"
    assert inv.last["window"]["transcript"] == "putting the PGT in Harmony"


def test_ac_per_10_speech_without_matching_catalog_item_yields_nothing(make_reconciler, inv):
    process(make_reconciler(), [[]], [], {}, transcript="I'm putting the banana in node 2.")
    assert [e for e in inv.last.get("events", []) if e["source"] == "speech"] == []


def test_ac_per_10_speech_without_location_yields_nothing(make_reconciler, inv):
    inv.add_entity(catalog_id="drill-pistol", embedding=VEC.vector_for_object("drill-1"), last_location="LAB1",
                   last_seen_at=T0 - timedelta(hours=1))
    process(make_reconciler(), [[]], [], {}, transcript="I'm putting the drill away.")
    assert [e for e in inv.last.get("events", []) if e["source"] == "speech"] == []


def test_ac_per_10_speech_never_creates_entity_from_free_text(make_reconciler, inv):
    process(make_reconciler(), [[]], [], {}, transcript="I'm putting the drill in node 2.")
    p = inv.last
    assert p["assignments"] == []
    assert [e for e in p.get("events", []) if e["source"] == "speech"] == []
    assert inv.entities == {}
