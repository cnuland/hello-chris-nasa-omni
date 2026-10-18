"""AC-PER-49 no crop ever overlaps a person (UI4-01, P1), AC-PER-50 station structure is not inventory (UI4-05),
AC-PER-51 event places (UI4-06), AC-PER-52 tests on real geometry and the real Gemma 4 replies.

The live case (docs/ui-review/round4): an unknown "window" with a misplaced omni box produced a 915x258 crop at the
top of a 1080x608 frame that showed a crew member's face and upper body."""
from __future__ import annotations

import glob
import json
from datetime import timedelta
from pathlib import Path

import pytest
from support import BUCKET, CAMERA_LOCATIONS, CATALOG, T0, FakeInventory, detections_for, obj, observation, scene_window

from perception.types import OmniResult

FIXTURES = Path(__file__).resolve().parent / "fixtures"
LAB_CAM = "cam-lab1-fwd"  # LAB1
HANDHELD = "cam-handheld"  # USOS (roaming)
FRAME_W, FRAME_H = 1080, 608  # the live frame


def px(x1, y1, x2, y2, w=FRAME_W, h=FRAME_H):
    """Pixel box on the live 1080x608 frame -> normalised box."""
    return (x1 / w, y1 / h, x2 / w, y2 / h)


LIVE_CROP = px(0, 0, 915, 258)  # the purged crop: 915x258 at the top of the frame
LIVE_PERSON = px(120, 20, 470, 600)  # a crew member (face + upper body) under it
CLEAR_LAPTOP = px(700, 380, 900, 520)  # a laptop away from everyone
PERSON_2 = px(760, 60, 1040, 600)


def run(rec, *, objects=(), frames=None, labels=None, people=(), events=(), t=0.0, summary="crew working",
        hint=None, dets=None, size=(FRAME_H, FRAME_W)):
    frames = frames if frames is not None else [[]]
    w = scene_window(frames, t0=T0 + timedelta(seconds=t), height=size[0], width=size[1])
    omni = OmniResult(status="ok", observation=observation(objects, people=people, events=events,
                                                           scene_summary=summary, location_hint=hint),
                      model="fake-omni", raw='{"raw": "reply"}')
    out = rec.process(w, omni, dets if dets is not None else detections_for(frames, labels or {}))
    assert out.status == "posted", out.error
    return out.payload, out.response


def crop_keys(storage):
    return [k for k in storage.objects if k.startswith("crops/")]


def by_label(payload, label):
    return [a for a in payload["assignments"]
            if (a.get("new_entity") or {}).get("label") == label]


# ------------------------------------------------------------------------------------------------ geometry
def test_ac_per_49_overlap_rule_live_geometry_is_an_overlap():
    from perception.privacy import overlaps_person

    assert overlaps_person(LIVE_CROP, [LIVE_PERSON])
    assert not overlaps_person(CLEAR_LAPTOP, [LIVE_PERSON])
    assert not overlaps_person(LIVE_CROP, [])


@pytest.mark.parametrize("crop,person,expected", [
    # IoU > 0.05 alone: a crop the size of the person, shifted half off
    (px(100, 300, 300, 600), px(250, 300, 450, 600), True),
    # intersection > 10 % of the crop alone (IoU tiny: the person is huge, the crop small, at the person's hip)
    (px(400, 500, 520, 600), px(0, 0, 450, 608), True),
    # head zone alone: a huge crop that holds a small person's face (IoU <= 0.05, < 10 % of the crop)
    (px(0, 0, 1080, 608), px(500, 100, 540, 260), True),
    # the person's legs only, a sliver (no rule): kept
    (px(0, 590, 160, 608), px(150, 100, 400, 608), False),
    # touching edges only: kept
    (px(0, 0, 300, 300), px(300, 0, 500, 300), False),
])
def test_ac_per_49_overlap_rule_partial_overlaps(crop, person, expected):
    from perception.privacy import overlaps_person

    assert overlaps_person(crop, [person]) is expected


def test_ac_per_49_each_rule_alone_triggers():
    from perception.privacy import head_zone_cover, intersection_over_crop, overlaps_person
    from perception.reconciler import iou

    a = (px(100, 300, 300, 600), px(250, 300, 450, 600))
    assert iou(*a) > 0.05
    b = (px(400, 500, 520, 600), px(0, 0, 450, 608))
    assert iou(*b) <= 0.05 and intersection_over_crop(*b) > 0.10
    c = (px(0, 0, 1080, 608), px(500, 100, 540, 260))
    assert iou(*c) <= 0.05 and intersection_over_crop(*c) <= 0.10 and head_zone_cover(*c) >= 0.25
    assert all(overlaps_person(crop, [p]) for crop, p in (a, b, c))


def test_ac_per_49_face_region_head_zone_is_the_whole_box():
    from perception.privacy import overlaps_person

    face = px(600, 400, 660, 470)
    # a large crop that covers the lower half of a face only: for a face, the whole box is the head zone
    assert overlaps_person(px(0, 435, 1080, 608), [face], kinds=["face"])


def test_ac_per_49_several_people_any_one_overlap_drops():
    from perception.privacy import overlaps_person

    assert overlaps_person(px(800, 100, 1000, 300), [LIVE_PERSON, PERSON_2])
    assert not overlaps_person(px(520, 200, 700, 400), [LIVE_PERSON, PERSON_2])


@pytest.mark.parametrize("label", ["face", "Face", "human face", "astronaut's hand", "hands", "arm", "head",
                                   "crew member's face"])
def test_ac_per_49_body_labels(label):
    from perception.privacy import is_body_label

    assert is_body_label(label)


@pytest.mark.parametrize("label", ["laptop", "hand drill", "handle", "headset", "facemask kit", "drill head bag",
                                   "cargo transfer bag", "robotic arm"])
def test_ac_per_49_not_body_labels(label):
    from perception.privacy import is_body_label

    assert not is_body_label(label)


# ------------------------------------------------------------------------------------------------ reconciler
def test_ac_per_49_live_case_misplaced_box_never_takes_the_person_box(make_reconciler, inv, storage):
    """Omni puts an unknown's box over the crew member; the detector only sees the person. Before: the unknown was
    IoU-linked to the person detection and its crop (the person) was stored and embedded."""
    frames = [[("crew-1", LIVE_PERSON)]]
    payload, _ = run(make_reconciler(LAB_CAM), frames=frames, labels={"crew-1": "person"},
                     objects=[obj("strange orb", LIVE_PERSON, description="round thing")])
    assert crop_keys(storage) == []
    orb = by_label(payload, "strange orb")
    assert len(orb) == 1
    a = orb[0]
    assert a["exemplar"] is None
    assert a["sighting"]["bbox_source"] == "omni"  # not the person detection's box
    for rv in payload["reviews"]:
        assert rv["image_uri"].startswith(f"s3://{BUCKET}/frames/")


def test_ac_per_49_live_case_crop_over_a_person_is_dropped(make_reconciler, inv, storage):
    """The live geometry with a detector box for the object: 915x258 at the top, over the person -> no crop, no
    exemplar, nothing uploaded under crops/, the sighting is flagged and the review shows the whole frame."""
    frames = [[("orb-1", LIVE_CROP), ("crew-1", LIVE_PERSON)]]
    payload, _ = run(make_reconciler(LAB_CAM), frames=frames, labels={"orb-1": "strange orb", "crew-1": "person"},
                     objects=[obj("strange orb", LIVE_CROP, description="round thing")])
    assert crop_keys(storage) == []
    a = by_label(payload, "strange orb")[0]
    assert a["exemplar"] is None
    assert a["sighting"]["person_overlap"] is True
    assert a["sighting"]["bbox"] == pytest.approx(list(LIVE_CROP), abs=1e-5)
    boxes = a["sighting"]["person_boxes"]
    assert any(b == pytest.approx(list(LIVE_PERSON), abs=1e-5) for b in boxes)
    reviews = [r for r in payload["reviews"] if r["entity_ref"] == a["ref"]]
    assert reviews and all(r["image_uri"].startswith(f"s3://{BUCKET}/frames/") for r in reviews)
    assert payload["window"]["person_crops_dropped"] == 1


def test_ac_per_49_known_object_partially_over_a_person_is_dropped(make_reconciler, inv, storage):
    box = px(380, 300, 560, 450)  # laptop held in front of the crew member's chest
    frames = [[("laptop-1", box), ("crew-1", LIVE_PERSON)]]
    payload, _ = run(make_reconciler(LAB_CAM), frames=frames, labels={"laptop-1": "laptop", "crew-1": "person"},
                     objects=[obj("laptop", box)])
    a = by_label(payload, "laptop")[0]
    assert a["exemplar"] is None and a["sighting"]["person_overlap"] is True
    assert crop_keys(storage) == []


def test_ac_per_49_crop_clear_of_everyone_is_kept(make_reconciler, inv, storage):
    frames = [[("laptop-1", CLEAR_LAPTOP), ("crew-1", LIVE_PERSON)]]
    payload, _ = run(make_reconciler(LAB_CAM), frames=frames, labels={"laptop-1": "laptop", "crew-1": "person"},
                     objects=[obj("laptop", CLEAR_LAPTOP)])
    a = by_label(payload, "laptop")[0]
    assert a["exemplar"] is not None and a["exemplar"]["crop_uri"]
    assert not a["sighting"].get("person_overlap")
    assert len(crop_keys(storage)) == 1
    assert payload["window"]["person_crops_dropped"] == 0


def test_ac_per_49_multiple_people_only_the_overlapping_crop_is_dropped(make_reconciler, inv, storage):
    over_2 = px(820, 150, 1000, 330)
    between = px(520, 200, 700, 400)
    frames = [[("laptop-1", over_2), ("tablet-1", between), ("crew-1", LIVE_PERSON), ("crew-2", PERSON_2)]]
    payload, _ = run(make_reconciler(LAB_CAM), frames=frames,
                     labels={"laptop-1": "laptop", "tablet-1": "tablet", "crew-1": "person", "crew-2": "person"},
                     objects=[obj("laptop", over_2), obj("tablet", between)])
    laptop, tablet = by_label(payload, "laptop")[0], by_label(payload, "tablet")[0]
    assert laptop["exemplar"] is None and laptop["sighting"]["person_overlap"] is True
    assert tablet["exemplar"] is not None and not tablet["sighting"].get("person_overlap")
    assert len(crop_keys(storage)) == 1


def test_ac_per_49_face_detection_is_a_region_and_never_an_object(make_reconciler, inv, storage):
    face = px(560, 120, 640, 220)
    box = px(500, 60, 760, 300)  # an object box around the face
    frames = [[("orb-1", box), ("face-1", face)]]
    payload, _ = run(make_reconciler(LAB_CAM), frames=frames, labels={"orb-1": "strange orb", "face-1": "face"},
                     objects=[obj("strange orb", box)])
    assert crop_keys(storage) == []
    labels = [(a.get("new_entity") or {}).get("label") for a in payload["assignments"]]
    assert "face" not in labels and len(payload["assignments"]) == 1


def test_ac_per_49_omni_person_box_is_a_region(make_reconciler, inv, storage):
    """No person detection; omni boxes the person (an object mapped to the person class) on frame 0."""
    box = px(380, 300, 560, 450)
    frames = [[("laptop-1", box)]]
    payload, _ = run(make_reconciler(LAB_CAM), frames=frames, labels={"laptop-1": "laptop"},
                     objects=[obj("laptop", box), obj("astronaut", LIVE_PERSON, description="blue shirt")])
    a = by_label(payload, "laptop")[0]
    assert a["exemplar"] is None and a["sighting"]["person_overlap"] is True
    assert crop_keys(storage) == []


def test_ac_per_49_omni_box_without_frame_index_counts_for_every_frame(make_reconciler, inv, storage):
    box = px(380, 300, 560, 450)
    frames = [[], [("laptop-1", box)]]
    payload, _ = run(make_reconciler(LAB_CAM), frames=frames, labels={"laptop-1": "laptop"},
                     objects=[obj("laptop", box, frame_index=1),
                              obj("astronaut", LIVE_PERSON, frame_index=None, description="blue shirt")])
    a = by_label(payload, "laptop")[0]
    assert a["exemplar"] is None and a["sighting"]["person_overlap"] is True


def test_ac_per_49_body_label_objects_are_not_instances(make_reconciler, inv, storage):
    hand = px(400, 300, 520, 420)
    frames = [[("hand-1", hand)]]
    payload, _ = run(make_reconciler(LAB_CAM), frames=frames, labels={"hand-1": "hand"},
                     objects=[obj("astronaut's hand", hand), obj("face", LIVE_PERSON)])
    assert payload["assignments"] == [] and payload["reviews"] == []
    assert crop_keys(storage) == []


def test_ac_per_49_person_detection_links_to_a_person_instance(make_reconciler, inv, storage):
    """A person object may still take the person detection's box (it is never cropped or embedded)."""
    frames = [[("crew-1", LIVE_PERSON)]]
    payload, _ = run(make_reconciler(LAB_CAM), frames=frames, labels={"crew-1": "person"},
                     objects=[obj("astronaut", None, description="blue shirt")])
    a = payload["assignments"][0]
    assert (a.get("new_entity") or {}).get("kind") == "person"
    assert a["exemplar"] is None  # persons are never embedded (AC-PER-36)
    assert a["sighting"]["bbox_source"] == "detector"  # a person may keep the person box


def test_ac_per_49_private_window_sends_no_person_boxes(make_reconciler, inv, storage):
    box = px(380, 300, 560, 450)
    frames = [[("laptop-1", box), ("crew-1", LIVE_PERSON)]]
    payload, _ = run(make_reconciler("cam-nod2-aft"), frames=frames,
                     labels={"laptop-1": "laptop", "crew-1": "person"}, objects=[obj("laptop", box)],
                     hint="Harmony crew quarters")
    assert payload["window"]["private"] is True
    for a in payload["assignments"]:
        assert "person_boxes" not in a["sighting"]
    assert storage.objects == {}


def test_ac_per_49_replay_queries_person_and_face():
    import perception.replay
    from perception.config import PerceptionConfig
    from perception.fakes import FakeDecision, FakeDetector, FakeEmbedder, FakeOmni, MemoryStorage, make_frame

    det = FakeDetector({})
    items = [(T0 + timedelta(seconds=i), make_frame([]), None) for i in range(6)]
    perception.replay.run(items, camera_id=LAB_CAM, start_time=T0, api=FakeInventory(),
                          omni=FakeOmni([observation([])] * 5), detector=det, embedder=FakeEmbedder(),
                          decision=FakeDecision(), asr=None, storage=MemoryStorage(BUCKET),
                          config=PerceptionConfig(camera_locations=dict(CAMERA_LOCATIONS), s3_bucket=BUCKET))
    assert det.calls
    for _frame, labels in det.calls:
        assert "person" in labels and "face" in labels
        assert "laptop" in labels


# ------------------------------------------------------------------------------------------------ AC-PER-50 structure
@pytest.mark.parametrize("label", ["hatch", "Hatchway", "hatch way", "window", "circular window", "handle",
                                   "hatch handle", "handrail", "hand rail", "wall", "panel", "control panel",
                                   "rack", "fabric cover", "green fabric cover", "light", "vent", "ceiling", "floor",
                                   "module", "space station", "spacecraft", "Dragon", "Earth", "station structure",
                                   "handle on the hatch", "window showing Earth", "docking port", "bracket", "cables"])
def test_ac_per_50_structure_labels(label):
    from perception.labels import is_structure_label

    assert is_structure_label(label)


@pytest.mark.parametrize("label", ["watch", "strange orb", "tool bag with handle", "laptop", "spacesuit",
                                   "hand drill", "multimeter", "glove"])
def test_ac_per_50_not_structure_labels(label):
    from perception.labels import is_structure_label

    assert not is_structure_label(label)


def _real_listings():
    objs = []
    d = json.loads((FIXTURES / "gemma4_tour_people_2026-10-18.json").read_text())
    for w in d["windows"]:
        objs += w["observation"].get("objects") or []
    for f in sorted(glob.glob(str(FIXTURES / "gemma4_tour" / "*.json"))):
        j = json.loads(Path(f).read_text())
        objs += (j.get("observation") or j).get("objects") or []
    return objs


def test_ac_per_50_real_gemma_unknowns_are_mostly_structure_and_no_catalog_match_is_removed():
    from perception import UNKNOWN
    from perception.labels import CatalogMapper, is_structure_label
    from perception.reconciler import structure_filtered

    mapper = CatalogMapper(CATALOG)
    listings = _real_listings()
    unknown = [o for o in listings if mapper.map(o["label"]).catalog_id == UNKNOWN]
    catalogued = [o for o in listings if mapper.map(o["label"]).catalog_id != UNKNOWN]
    removed = [o for o in unknown if is_structure_label(o["label"])]
    kept = [o for o in unknown if not is_structure_label(o["label"])]
    assert len(unknown) == 19 and len(removed) == 17
    assert {o["label"] for o in kept} == {"watch"}
    assert len({o["label"] for o in unknown}) == 8 and len({o["label"] for o in removed}) == 7
    # the reconciler's filter never removes a listing that maps to a catalog class
    assert structure_filtered(catalogued, mapper) == []
    assert len(structure_filtered(listings, mapper)) == 17


def test_ac_per_50_catalog_names_and_synonyms_are_never_filtered():
    from perception.labels import CatalogMapper
    from perception.reconciler import structure_filtered

    mapper = CatalogMapper(CATALOG)
    names = [c["class_name"] for c in CATALOG] + [s for c in CATALOG for s in c.get("synonyms") or []]
    assert structure_filtered([{"label": n} for n in names], mapper) == []


def test_ac_per_50_structure_creates_nothing_and_is_counted(make_reconciler, inv, storage):
    hatch = px(300, 250, 1000, 600)
    lap = CLEAR_LAPTOP
    frames = [[("hatch-1", hatch), ("laptop-1", lap)]]
    payload, _ = run(make_reconciler(LAB_CAM), frames=frames, labels={"hatch-1": "hatch", "laptop-1": "laptop"},
                     objects=[obj("hatch", hatch, description="circular metallic hatch"), obj("laptop", lap),
                              obj("window", None, description="circular window showing Earth below"),
                              obj("spacecraft", None, description="spacecraft outside the window")],
                     events=[{"type": "used", "object_label": "hatch", "frame_index": 0, "confidence": 0.8}])
    labels = [(a.get("new_entity") or {}).get("label") for a in payload["assignments"]]
    assert labels == ["laptop"]
    assert payload["reviews"] == [] and payload["events"] == []
    assert payload["window"]["structure_filtered"] == 3
    assert len(crop_keys(storage)) == 1  # the laptop's only


def test_ac_per_50_catalogued_cable_is_kept(make_reconciler, inv):
    payload, _ = run(make_reconciler(LAB_CAM), objects=[obj("cable", CLEAR_LAPTOP)])
    assert payload["window"]["structure_filtered"] == 0
    assert [s["catalog_id"] for s in payload["stock_updates"]] == ["power-cable"]


def test_ac_per_50_replay_stats_sum_structure():
    import perception.replay
    from perception.config import PerceptionConfig
    from perception.fakes import FakeDecision, FakeDetector, FakeEmbedder, FakeOmni, MemoryStorage, make_frame

    items = [(T0 + timedelta(seconds=i), make_frame([]), None) for i in range(9)]
    stats = perception.replay.run(
        items, camera_id=LAB_CAM, start_time=T0, api=FakeInventory(),
        omni=FakeOmni([observation([obj("hatch", None), obj("window", None)])] * 5), detector=FakeDetector({}),
        embedder=FakeEmbedder(), decision=FakeDecision(), asr=None, storage=MemoryStorage(BUCKET),
        config=PerceptionConfig(camera_locations=dict(CAMERA_LOCATIONS), s3_bucket=BUCKET))
    assert stats.posted == 3 and stats.structure_filtered == 6


def test_ac_per_50_prompt_names_structure_and_people():
    from perception.omni import build_system_prompt

    p = build_system_prompt(["laptop"])
    for word in ("station structure", "hatches", "windows", "handles", "Earth", "spacecraft"):
        assert word in p
    assert "people go in people" in p


# ------------------------------------------------------------------------------------------------ AC-PER-51 places
def _person(inv, description, location, ago_s=60.0):
    return inv.add_entity(catalog_id="crew-member", kind="person", description=description, last_location=location,
                          last_seen_at=T0 - timedelta(seconds=ago_s))


MOVED = {"type": "moved", "object_label": "person", "frame_index": 0, "confidence": 0.8}


def test_ac_per_51_person_left_usos_while_still_in_usos_is_not_emitted(make_reconciler, inv):
    """Live: "Person 737f632b left US Orbital Segment (USOS)" while its sightings were still in USOS."""
    eid = _person(inv, "blue shirt, white gloves", "USOS")
    payload, _ = run(make_reconciler(HANDHELD), people=[{"ref": "P1", "appearance": "blue shirt, white gloves"}],
                            events=[MOVED], summary="crew member floating")
    a = payload["assignments"][0]
    assert a["entity_id"] == eid and a["sighting"]["location_id"] == "USOS"
    assert payload["events"] == []


def test_ac_per_51_person_moved_to_another_module_names_the_destination(make_reconciler, inv):
    eid = _person(inv, "blue shirt, white gloves", "NOD1", ago_s=120)
    payload, _ = run(make_reconciler(LAB_CAM), people=[{"ref": "P1", "appearance": "blue shirt, white gloves"}],
                     events=[MOVED])
    assert payload["assignments"][0]["entity_id"] == eid
    (ev,) = payload["events"]
    assert ev["from_location"] == "NOD1" and ev["to_location"] == "LAB1"


def test_ac_per_51_person_moved_within_the_segment_is_not_a_move(make_reconciler, inv):
    """USOS -> LAB1 is not leaving USOS (LAB1 is inside it)."""
    _person(inv, "blue shirt, white gloves", "USOS", ago_s=120)
    payload, _ = run(make_reconciler(LAB_CAM), people=[{"ref": "P1", "appearance": "blue shirt, white gloves"}],
                     events=[MOVED])
    assert payload["events"] == []


def test_ac_per_51_new_person_track_event_gets_its_place(make_reconciler, inv):
    payload, _ = run(make_reconciler(LAB_CAM), people=[{"ref": "P1", "appearance": "red shirt"}],
                     events=[MOVED, {"type": "seen", "object_label": "person", "frame_index": 0, "confidence": 0.8}])
    by_type = {e["event_type"]: e for e in payload["events"]}
    assert by_type["seen"]["to_location"] == "LAB1"
    assert by_type["moved"]["from_location"] is None and by_type["moved"]["to_location"] == "LAB1"


def test_ac_per_51_object_events_carry_the_known_place(make_reconciler, inv):
    from perception.fakes import FakeEmbedder

    eid = inv.add_entity(catalog_id="drill-pistol", last_location="NOD1", last_seen_at=T0 - timedelta(seconds=900),
                         embedding=FakeEmbedder().vector_for_object("drill-1"))
    frames = [[("drill-1", CLEAR_LAPTOP)]]
    payload, _ = run(make_reconciler(LAB_CAM, scorer=lambda f: 0.95), frames=frames, labels={"drill-1": "drill"},
                     objects=[obj("drill", CLEAR_LAPTOP, frame_index=0, held_by="P1")],
                     people=[{"ref": "P1", "appearance": "green shirt"}],
                     events=[{"type": "moved", "object_label": "drill", "actor_ref": "P1", "frame_index": 0,
                              "confidence": 0.8, "to_location_hint": "somewhere over there"},
                             {"type": "used", "object_label": "drill", "actor_ref": "P1", "frame_index": 0,
                              "confidence": 0.8}])
    drill = next(a for a in payload["assignments"] if a.get("entity_id") == eid)
    assert drill["sighting"]["location_id"] == "LAB1"
    by_type = {e["event_type"]: e for e in payload["events"]}
    assert by_type["moved"]["from_location"] == "NOD1" and by_type["moved"]["to_location"] == "LAB1"
    assert by_type["used"]["from_location"] == "NOD1" and by_type["used"]["to_location"] == "LAB1"


def test_ac_per_51_object_moved_and_seen_in_the_same_place_keeps_no_destination(make_reconciler, inv):
    eid = inv.add_entity(catalog_id="drill-pistol", last_location="LAB1", last_seen_at=T0 - timedelta(seconds=30))
    payload, _ = run(make_reconciler(LAB_CAM), objects=[obj("drill", None, frame_index=0)],
                     events=[{"type": "moved", "object_label": "drill", "frame_index": 0, "confidence": 0.8}])
    assert next(a for a in payload["assignments"] if a.get("entity_id") == eid)
    (ev,) = payload["events"]
    assert ev["from_location"] == "LAB1" and ev["to_location"] is None


@pytest.mark.parametrize("etype", ["removed", "consumed"])
def test_ac_per_51_leaving_events_get_no_destination(make_reconciler, inv, etype):
    payload, _ = run(make_reconciler(LAB_CAM), objects=[obj("drill", None, frame_index=0)],
                     events=[{"type": etype, "object_label": "drill", "frame_index": 0, "confidence": 0.8}])
    (ev,) = payload["events"]
    assert ev["to_location"] is None


def test_ac_per_49_catalogued_item_with_a_body_word_is_kept(inv, storage, embedder, config):
    """Review round: the body-label filter never drops a catalogued (non-person) class, e.g. a "robot hand" kit."""
    from support import LOCATIONS

    from perception.fakes import FakeDecision
    from perception.reconciler import Reconciler

    catalog = [*CATALOG, {"catalog_id": "robot-hand", "class_name": "robot hand", "display_name": "Robot hand",
                          "synonyms": [], "category": "object", "is_fungible": False, "expected_count": None}]
    inv.catalog = catalog
    rec = Reconciler(api=inv, embedder=embedder, decision=FakeDecision(), storage=storage, config=config,
                     camera_id=LAB_CAM, catalog=catalog, locations=LOCATIONS)
    frames = [[("hand-1", CLEAR_LAPTOP)]]
    payload, _ = run(rec, frames=frames, labels={"hand-1": "robot hand"}, objects=[obj("robot hand", CLEAR_LAPTOP)])
    a = payload["assignments"][0]
    assert a["new_entity"]["catalog_id"] == "robot-hand" and a["exemplar"] is not None


# ------------------------------------------------------------------------------------------------ review round
def test_review_unknown_never_takes_a_person_box_by_label_or_iou(make_reconciler, inv, storage):
    """An UNKNOWN whose box agrees with the person detection, plus a label link attempt on a 'figure' detection."""
    frames = [[("crew-1", LIVE_PERSON)]]
    payload, _ = run(make_reconciler(LAB_CAM), frames=frames, labels={"crew-1": "person"},
                     objects=[obj("odd figure thing", LIVE_PERSON)])
    for a in payload["assignments"]:
        assert a["sighting"]["bbox_source"] != "detector" and a["exemplar"] is None
    assert crop_keys(storage) == []


def test_review_head_box_is_a_region_even_though_head_fuzzy_maps_to_headset(make_reconciler, inv, storage):
    """Reviewer repro: "head" fuzzy-maps to the headset class; the headset crop over the head was uploaded."""
    head = px(200, 40, 330, 200)
    frames = [[("headset-1", head)]]
    payload, _ = run(make_reconciler(LAB_CAM), frames=frames, labels={"headset-1": "headset"},
                     objects=[obj("head", head), obj("headset", head)])
    assert crop_keys(storage) == []
    labels = [(a.get("new_entity") or {}).get("label") for a in payload["assignments"]]
    assert labels == ["headset"]
    assert payload["assignments"][0]["sighting"]["person_overlap"] is True


@pytest.mark.parametrize("label", ["the man", "a woman", "the head", "human figure", "a silhouette",
                                   "the astronaut's face"])
def test_review_determiners_do_not_hide_a_body_label(label):
    from perception.privacy import is_body_label

    assert is_body_label(label)


def test_review_the_man_box_protects_the_laptop_in_his_hand(make_reconciler, inv, storage):
    box = px(380, 300, 560, 450)
    frames = [[("laptop-1", box)]]
    payload, _ = run(make_reconciler(LAB_CAM), frames=frames, labels={"laptop-1": "laptop"},
                     objects=[obj("laptop", box), obj("the man", LIVE_PERSON)])
    assert crop_keys(storage) == []
    assert [(a.get("new_entity") or {}).get("label") for a in payload["assignments"]] == ["laptop"]


def test_review_face_regions_send_their_kind_and_boxes_are_ordered():
    """person_boxes carry person_box_kinds so inventory-api applies the face rule too; inverted boxes are sorted."""
    from perception.privacy import overlaps_person

    assert overlaps_person((0.2, 0.17, 0.7, 0.6), [(0.4, 0.1, 0.5, 0.2)], ["face"])
    assert overlaps_person((0.2, 0.2, 0.6, 0.6), [(0.5, 0.9, 0.3, 0.3)])  # inverted person box
    assert overlaps_person((0.3, 0.3, 0.3, 0.5), [(0.0, 0.0, 1.0, 1.0)])  # zero-area crop fails closed


def test_review_payload_sends_person_box_kinds(make_reconciler, inv, storage):
    face = px(560, 120, 640, 220)
    frames = [[("laptop-1", CLEAR_LAPTOP), ("face-1", face), ("crew-1", LIVE_PERSON)]]
    payload, _ = run(make_reconciler(LAB_CAM), frames=frames,
                     labels={"laptop-1": "laptop", "face-1": "face", "crew-1": "person"},
                     objects=[obj("laptop", CLEAR_LAPTOP)])
    s = payload["assignments"][0]["sighting"]
    assert len(s["person_boxes"]) == len(s["person_box_kinds"]) == 2
    assert sorted(s["person_box_kinds"]) == ["face", "person"]


def test_review_person_boxes_overlapping_the_object_are_sent_first(make_reconciler, inv, storage):
    box = px(380, 300, 560, 450)
    far = [(f"crew-{i}", px(900 + i, 0, 905 + i * 2, 50)) for i in range(12)]
    frames = [[*far, ("laptop-1", box)]]
    labels = {k: "person" for k, _ in far} | {"laptop-1": "laptop"}
    payload, _ = run(make_reconciler(LAB_CAM), frames=frames, labels=labels,
                     objects=[obj("laptop", box), obj("astronaut", LIVE_PERSON, description="blue shirt")])
    laptop = by_label(payload, "laptop")[0]["sighting"]
    assert len(laptop["person_boxes"]) == 10
    assert laptop["person_boxes"][0] == pytest.approx(list(LIVE_PERSON), abs=1e-5)


@pytest.mark.parametrize("label", ["memory module", "battery module", "portable fan", "camera mount",
                                   "laptop cover", "picture frame"])
def test_review_loose_items_with_broad_head_nouns_are_kept(label):
    from perception.labels import is_structure_label

    assert not is_structure_label(label)


@pytest.mark.parametrize("label", ["module", "the module", "fan", "mount", "frame", "cover", "robot arm",
                                   "robotic arm", "the window"])
def test_review_broad_nouns_alone_are_still_structure(label):
    from perception.labels import is_structure_label

    assert is_structure_label(label)


def test_review_filled_destinations_do_not_break_cross_window_dedup(make_reconciler, inv):
    """The same 'used' seen by two overlapping windows, the object's box centre in two zones of LAB1 -> one event."""
    from perception.fakes import FakeEmbedder

    rec = make_reconciler(LAB_CAM, scorer=lambda f: 0.95, camera_zones={LAB_CAM: {
        "LAB1S2": (0.0, 0.0, 0.5, 1.0), "LAB1P3": (0.5, 0.0, 1.0, 1.0)}})
    eid = inv.add_entity(catalog_id="drill-pistol", last_location="LAB1", last_seen_at=T0 - timedelta(seconds=900),
                         embedding=FakeEmbedder().vector_for_object("drill-1"))
    ev = [{"type": "used", "object_label": "drill", "frame_index": 0, "confidence": 0.8}]
    left, right = px(300, 300, 500, 400), px(560, 300, 760, 400)
    p1, _ = run(rec, frames=[[("drill-1", left)]], labels={"drill-1": "drill"}, objects=[obj("drill", left)],
                events=ev)
    p2, _ = run(rec, frames=[[("drill-1", right)]], labels={"drill-1": "drill"}, objects=[obj("drill", right)],
                events=ev, t=4)
    assert [a.get("entity_id") for a in p1["assignments"]] == [eid]
    assert len(p1["events"]) == 1 and p2["events"] == []
