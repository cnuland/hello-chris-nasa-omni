"""UI review round 4 (docs/ui-review/round4): AC-API-41 no crop over a person (UI4-01, P1), AC-API-42 event context
(UI4-06), AC-API-43 segment-level items in /stats (UI4-07), AC-API-44 snapshot_withheld_reason (UI4-10).

Real geometry from the live case: a 915x258 crop at the top of a 1080x608 frame over a crew member."""
from __future__ import annotations

import copy
from datetime import timedelta

import pytest

from inventory_api.migrations import apply_migrations
from tests.conftest import (
    BUCKET,
    CONTRACTS_DB,
    audit_rows,
    iso,
    make_entity,
    make_event,
    make_review,
    make_sighting,
    make_window,
    ok,
    table_counts,
    ts,
    vec,
)

FRAME = f"s3://{BUCKET}/frames/cam-lab1-fwd/0001.jpg"
CROP = f"s3://{BUCKET}/crops/0001-a.jpg"
W, H = 1080, 608


def px(x1, y1, x2, y2):
    return [round(x1 / W, 6), round(y1 / H, 6), round(x2 / W, 6), round(y2 / H, 6)]


LIVE_CROP = px(0, 0, 915, 258)
LIVE_PERSON = px(120, 20, 470, 600)
PERSON_2 = px(760, 60, 1040, 600)
CLEAR = px(700, 380, 900, 520)


def payload(*, bbox=LIVE_CROP, person_boxes=None, person_overlap=None, frame=FRAME, kind="object",
            catalog_id="laptop", review_image=CROP, location="LAB1S2"):
    sighting = {"location_id": location, "seen_at": iso(10), "bbox": bbox, "bbox_source": "detector",
                "confidence": 0.8, "description": "an object"}
    if frame is not None:
        sighting["frame_uri"] = frame
    if person_boxes is not None:
        sighting["person_boxes"] = person_boxes
    if person_overlap is not None:
        sighting["person_overlap"] = person_overlap
    body = {
        "window": {"camera_id": "cam-lab1-fwd", "t_start": iso(10), "t_end": iso(10.1), "scene_summary": "crew",
                   "transcript": "", "model": "gemma4", "raw_uri": f"s3://{BUCKET}/raw/w1.json"},
        "assignments": [{
            "ref": "o1",
            "new_entity": {"catalog_id": catalog_id, "kind": kind, "status": "unknown" if catalog_id is None
                           else "provisional", "label": "window" if catalog_id is None else "laptop"},
            "sighting": sighting,
            "exemplar": {"embedding": vec((0, 1.0)), "quality": 0.7, "crop_uri": CROP},
        }],
        "events": [],
        "reviews": [],
        "decisions": [],
    }
    if review_image is not None:
        body["reviews"] = [{"kind": "unknown_object", "entity_ref": "o1", "candidate_ids": [],
                            "image_uri": review_image, "description": "window: circular window showing Earth",
                            "observed_at": iso(10), "location_id": location}]
    return body


def _ingest(client, body):
    return client.post("/ingest/window", json=body)


def _entity(client, body):
    r = _ingest(client, body)
    assert r.status_code == 201, r.text
    return r.json()["entities"]["o1"]


def _state(db, eid):
    return {
        "exemplars": db.execute("SELECT crop_uri, sighting_id FROM entity_exemplar WHERE entity_id=%s",
                                (eid,)).fetchall(),
        "snapshot": db.execute("SELECT snapshot_uri FROM entity WHERE entity_id=%s", (eid,)).fetchone()[0],
        "overlap": [r[0] for r in db.execute("SELECT person_overlap FROM sighting WHERE entity_id=%s", (eid,))],
        "review_images": [r[0] for r in db.execute("SELECT image_uri FROM review_item WHERE entity_id=%s", (eid,))],
    }


# ------------------------------------------------------------------ AC-API-41 ingest
def test_ac_api_41_live_crop_over_a_person_is_not_stored(client, db):
    eid = _entity(client, payload(catalog_id=None, person_boxes=[LIVE_PERSON]))
    st = _state(db, eid)
    assert st["exemplars"] == []
    assert st["snapshot"] == FRAME
    assert st["overlap"] == [True]
    assert st["review_images"] == [FRAME]  # the whole frame, never the crop


def test_ac_api_41_client_flag_alone_drops_the_crop(client, db):
    eid = _entity(client, payload(person_overlap=True, bbox=CLEAR))
    st = _state(db, eid)
    assert st["exemplars"] == [] and st["snapshot"] == FRAME and st["overlap"] == [True]


def test_ac_api_41_crop_clear_of_people_is_kept_and_linked_to_its_sighting(client, db):
    eid = _entity(client, payload(bbox=CLEAR, person_boxes=[LIVE_PERSON, px(20, 300, 110, 600)]))
    st = _state(db, eid)
    sid = db.execute("SELECT sighting_id FROM sighting WHERE entity_id=%s", (eid,)).fetchone()[0]
    assert st["exemplars"] == [(CROP, sid)]
    assert st["snapshot"] == CROP and st["overlap"] == [False]
    assert st["review_images"] == [CROP]


@pytest.mark.parametrize("bbox,boxes", [
    (px(820, 150, 1000, 330), [LIVE_PERSON, PERSON_2]),  # over the second of two people
    (px(380, 300, 560, 450), [LIVE_PERSON]),  # partly over the chest
    (px(0, 0, 1080, 608), [px(500, 100, 540, 260)]),  # a whole-frame crop that holds a small person's face
])
def test_ac_api_41_partial_overlaps_and_several_people(client, db, bbox, boxes):
    eid = _entity(client, payload(bbox=bbox, person_boxes=boxes))
    st = _state(db, eid)
    assert st["exemplars"] == [] and st["overlap"] == [True] and st["snapshot"] == FRAME


def test_ac_api_41_crop_without_bbox_but_with_person_boxes_is_dropped(client, db):
    eid = _entity(client, payload(bbox=None, person_boxes=[LIVE_PERSON]))
    assert _state(db, eid)["exemplars"] == []


def test_ac_api_41_review_on_a_dropped_crop_without_frame_is_422_and_nothing_written(client, db):
    before = table_counts(db)
    r = _ingest(client, payload(frame=None, person_boxes=[LIVE_PERSON]))
    assert r.status_code == 422, r.text
    assert table_counts(db) == before


@pytest.mark.parametrize("boxes", [
    [[0.1, 0.2, 0.3]],  # wrong length
    [[0.1, 0.2, 1.5, 0.4]],  # outside 0..1
    [[0.1, 0.2, 0.3, 0.4]] * 21,  # too many
    [["a", 0.2, 0.3, 0.4]],
])
def test_ac_api_41_invalid_person_boxes_are_422(client, db, boxes):
    before = table_counts(db)
    assert _ingest(client, payload(person_boxes=boxes)).status_code == 422
    assert table_counts(db) == before


def test_ac_api_41_person_entities_never_store_an_exemplar_or_crop(client, db):
    body = payload(kind="person", catalog_id="crew-member", bbox=CLEAR, review_image=None)
    eid = _entity(client, body)
    st = _state(db, eid)
    assert st["exemplars"] == [] and st["snapshot"] == FRAME


def test_ac_api_41_person_overlap_is_not_a_client_override_to_false(client, db):
    """person_overlap=false never overrides the API's own geometric check."""
    eid = _entity(client, payload(person_overlap=False, person_boxes=[LIVE_PERSON]))
    assert _state(db, eid)["exemplars"] == []


def _overlap_entity_with_linked_exemplars(db):
    eid = make_entity(db, label="window", status="unknown", last_location="LAB1", last_seen_at=ts(10))
    bad = make_sighting(db, eid, seen_at=ts(10), location_id="LAB1", frame_uri=FRAME)
    good = make_sighting(db, eid, seen_at=ts(11), location_id="LAB1", frame_uri=FRAME)
    db.execute("UPDATE sighting SET person_overlap = TRUE WHERE sighting_id = %s", (bad,))
    ids = {}
    for name, sid, axis in (("bad", bad, 0), ("good", good, 1)):
        v = "[" + ",".join("1" if i == axis else "0" for i in range(768)) + "]"
        ids[name] = db.execute(
            "INSERT INTO entity_exemplar (entity_id, embedding, quality, crop_uri, sighting_id) "
            "VALUES (%s, %s::halfvec, 0.7, %s, %s) RETURNING exemplar_id",
            (eid, v, f"s3://{BUCKET}/crops/{name}.jpg", sid)).fetchone()[0]
    rid = make_review(db, entity_id=eid, image_uri=FRAME, location_id="LAB1", observed_at=ts(10))
    return eid, rid, ids


@pytest.mark.parametrize("action", ["assign_catalog", "new_catalog"])
def test_ac_api_41_classify_never_promotes_a_person_overlap_exemplar(client, db, action):
    eid, rid, ids = _overlap_entity_with_linked_exemplars(db)
    body = {"action": action, "operator": "amy"}
    if action == "assign_catalog":
        body["catalog_id"] = "laptop"
    else:
        body["new_catalog"] = {"catalog_id": "porthole-cover", "class_name": "porthole cover",
                               "display_name": "Porthole cover"}
    ok(client.post(f"/review/{rid}/resolve", json=body))
    left = [r[0] for r in db.execute("SELECT exemplar_id FROM entity_exemplar WHERE entity_id=%s", (eid,))]
    assert left == [ids["good"]]
    classify = [a for a in audit_rows(db, "classify")][-1]
    assert classify[3]["withheld_exemplar_ids"] == [ids["bad"]]


def test_ac_api_41_item_detail_sightings_carry_person_overlap_and_is_private(client, db):
    eid = _entity(client, payload(person_boxes=[LIVE_PERSON]))
    private_w = make_window(db, camera_id="cam-nod2-aft", t_start=ts(20))
    db.execute("UPDATE observation_window SET is_private = TRUE, privacy_reason = 'private_zone' "
               "WHERE window_id = %s", (private_w,))
    make_sighting(db, eid, seen_at=ts(20), location_id="NOD2", window_id=private_w)
    item = ok(client.get(f"/items/{eid}"))
    by_time = {s["seen_at"][:16]: s for s in item["sightings"]}
    newest, oldest = item["sightings"][0], item["sightings"][-1]
    assert newest["is_private"] is True and newest["person_overlap"] is False
    assert oldest["is_private"] is False and oldest["person_overlap"] is True
    assert len(by_time) == 2


def test_ac_api_41_merge_and_unmerge_keep_the_exemplar_sighting_link(client, db):
    src = _entity(client, payload(bbox=CLEAR, review_image=None))
    tgt = make_entity(db, label="laptop", catalog_id="laptop", last_location="LAB1", last_seen_at=ts(5))
    link = db.execute("SELECT exemplar_id, sighting_id FROM entity_exemplar WHERE entity_id=%s", (src,)).fetchone()
    assert link[1] is not None
    ok(client.post(f"/items/{src}/merge", json={"into_entity_id": tgt, "operator": "amy"}))
    assert db.execute("SELECT sighting_id FROM entity_exemplar WHERE exemplar_id=%s", (link[0],)).fetchone()[0] \
        == link[1]
    ok(client.post(f"/items/{src}/unmerge", json={"operator": "amy"}))
    row = db.execute("SELECT entity_id::text, sighting_id FROM entity_exemplar WHERE exemplar_id=%s",
                     (link[0],)).fetchone()
    assert row == (src, link[1])


def test_ac_api_41_migration_006_is_idempotent(database_url, db):
    for _ in range(2):
        apply_migrations(database_url, CONTRACTS_DB)
    cols = {(r[0], r[1]) for r in db.execute(
        "SELECT table_name, column_name FROM information_schema.columns WHERE table_schema = current_schema() "
        "AND ((table_name = 'sighting' AND column_name = 'person_overlap') "
        "  OR (table_name = 'entity_exemplar' AND column_name = 'sighting_id'))")}
    assert cols == {("sighting", "person_overlap"), ("entity_exemplar", "sighting_id")}


def test_ac_api_41_e2e_style_window_with_person_and_overlapping_box_stores_no_crop(client, db, store):
    """A whole window as perception sends it: a person sighting plus an object whose box overlaps the person."""
    body = payload(person_boxes=[LIVE_PERSON])
    person = copy.deepcopy(body["assignments"][0])
    person.update(ref="p1", exemplar=None,
                  new_entity={"catalog_id": "crew-member", "kind": "person", "status": "provisional",
                              "label": "person", "description": "blue shirt"})
    person["sighting"]["bbox"] = LIVE_PERSON
    body["assignments"].append(person)
    r = _ingest(client, body)
    assert r.status_code == 201, r.text
    assert db.execute("SELECT count(*) FROM entity_exemplar").fetchone()[0] == 0
    assert db.execute("SELECT count(*) FROM entity WHERE snapshot_uri LIKE '%%/crops/%%'").fetchone()[0] == 0
    assert db.execute("SELECT count(*) FROM review_item WHERE image_uri LIKE '%%/crops/%%'").fetchone()[0] == 0


# ------------------------------------------------------------------ AC-API-42 events
def test_ac_api_42_events_carry_appearance_and_entity_location(client, db):
    p = make_entity(db, label="person", kind="person", catalog_id="crew-member",
                    description="blue shirt and white gloves", last_location="USOS", last_seen_at=ts(12))
    o = make_entity(db, label="drill", catalog_id="drill-pistol", description="black drill",
                    last_location="LAB1S2", last_seen_at=ts(12))
    make_event(db, p, event_type="seen", occurred_at=ts(10), to_location="USOS")
    make_event(db, o, event_type="used", occurred_at=ts(11), to_location="LAB1S2", actor_desc="green shirt")
    rows = {r["entity_id"]: r for r in ok(client.get("/events"))}
    assert rows[p]["entity_appearance"] == "blue shirt and white gloves"
    assert rows[p]["entity_last_location"] == "USOS"
    assert rows[p]["entity_last_location_name"] == "US Orbital Segment"
    assert rows[o]["entity_appearance"] is None
    assert rows[o]["entity_last_location"] == "LAB1S2"
    assert rows[o]["entity_last_location_name"] == "Lab starboard rack 2"
    assert rows[o]["location_source"] == "event"


def test_ac_api_42_seen_event_without_place_takes_the_sighting_place(client, db):
    p = make_entity(db, label="person", kind="person", catalog_id="crew-member", description="red shirt",
                    last_location="CUP1", last_seen_at=ts(10))
    make_sighting(db, p, seen_at=ts(10) + timedelta(seconds=2), location_id="CUP1")
    make_event(db, p, event_type="seen", occurred_at=ts(10))
    (ev,) = ok(client.get("/events", params={"entity_id": p}))
    assert ev["to_location"] == "CUP1" and ev["to_location_name"] == "Cupola"
    assert ev["location_source"] == "sighting" and ev["location_role"] == "at"


def test_ac_api_42_person_left_usos_while_seen_in_usos_reads_as_seen_there(client, db):
    """Live: "Person 737f632b left US Orbital Segment (USOS)" while its sightings were still in USOS."""
    p = make_entity(db, label="person", kind="person", catalog_id="crew-member", description="blue shirt",
                    last_location="USOS", last_seen_at=ts(12))
    make_sighting(db, p, seen_at=ts(10), location_id="USOS")
    make_event(db, p, event_type="moved", occurred_at=ts(10), from_location="USOS")
    (ev,) = ok(client.get("/events", params={"entity_id": p}))
    assert ev["from_location"] == "USOS" and ev["to_location"] == "USOS"
    assert ev["location_role"] == "at" and ev["location_source"] == "sighting"


def test_ac_api_42_object_moved_without_destination_takes_the_next_sighting(client, db):
    o = make_entity(db, label="camera", catalog_id="camera-dslr", last_location="NOD2", last_seen_at=ts(11))
    make_sighting(db, o, seen_at=ts(10) + timedelta(seconds=30), location_id="NOD2")
    make_sighting(db, o, seen_at=ts(30), location_id="LAB1")  # far away in time: not used
    make_event(db, o, event_type="moved", occurred_at=ts(10), from_location="LAB1")
    (ev,) = ok(client.get("/events", params={"entity_id": o}))
    assert ev["to_location"] == "NOD2" and ev["location_role"] == "from_to"
    assert ev["location_source"] == "sighting"


def test_ac_api_42_no_sighting_nearby_leaves_no_place(client, db):
    o = make_entity(db, label="drill", catalog_id="drill-pistol")
    make_sighting(db, o, seen_at=ts(20), location_id="LAB1")  # 10 minutes away
    make_event(db, o, event_type="used", occurred_at=ts(10))
    (ev,) = ok(client.get("/events", params={"entity_id": o}))
    assert ev["to_location"] is None and ev["location_source"] is None and ev["location_role"] is None


@pytest.mark.parametrize("etype", ["removed", "consumed"])
def test_ac_api_42_leaving_events_take_no_destination(client, db, etype):
    o = make_entity(db, label="tablet", catalog_id="tablet")
    make_sighting(db, o, seen_at=ts(10), location_id="LAB1P3")
    make_event(db, o, event_type=etype, occurred_at=ts(10))
    (ev,) = ok(client.get("/events", params={"entity_id": o}))
    assert ev["to_location"] is None and ev["location_source"] is None


def test_ac_api_42_item_detail_events_carry_the_same_fields(client, db):
    p = make_entity(db, label="person", kind="person", catalog_id="crew-member", description="red shirt",
                    last_location="CUP1", last_seen_at=ts(10))
    make_sighting(db, p, seen_at=ts(10), location_id="CUP1")
    make_event(db, p, event_type="seen", occurred_at=ts(10))
    (ev,) = ok(client.get(f"/items/{p}"))["events"]
    assert ev["entity_appearance"] == "red shirt" and ev["to_location"] == "CUP1"
    assert ev["entity_last_location"] == "CUP1" and ev["entity_last_location_name"] == "Cupola"


# ------------------------------------------------------------------ AC-API-43 stats
def test_ac_api_43_stats_count_segment_level_items(client, db):
    for loc in ("USOS", "USOS", "ROS", "LAB1S2", "NOD2", None):
        make_entity(db, label="laptop", catalog_id="laptop", last_location=loc, last_seen_at=ts(1))
    make_entity(db, label="laptop", catalog_id="laptop", last_location="USOS", status="dismissed")
    make_entity(db, label="person", kind="person", catalog_id="crew-member", last_location="USOS")
    s = ok(client.get("/stats"))
    assert s["by_module"] == {"USOS": 3, "ROS": 1, "LAB1": 1, "NOD2": 1}
    assert s["by_location_level"] == {"module": 2, "segment": 4, "unknown": 1}
    assert sum(s["by_location_level"].values()) == sum(s["by_kind"].values()) == 7
    assert sum(s["by_module"].values()) == s["by_location_level"]["module"] + s["by_location_level"]["segment"]


def test_ac_api_43_stats_empty(client, db):
    s = ok(client.get("/stats"))
    assert s["by_location_level"] == {"module": 0, "segment": 0, "unknown": 0}
    assert s["by_module"] == {}


# ------------------------------------------------------------------ AC-API-44 snapshot_withheld_reason
def test_ac_api_44_items_first_seen_in_a_private_window_say_why_there_is_no_image(client, db):
    w = make_window(db, camera_id="cam-nod2-aft", t_start=ts(5))
    db.execute("UPDATE observation_window SET is_private = TRUE, privacy_reason = 'privacy_hold' "
               "WHERE window_id = %s", (w,))
    private = make_entity(db, label="laptop", catalog_id="laptop", last_location="NOD2", last_seen_at=ts(5))
    make_sighting(db, private, seen_at=ts(5), location_id="NOD2", window_id=w)
    plain = make_entity(db, label="laptop", catalog_id="laptop", last_location="LAB1", last_seen_at=ts(4))
    pictured = make_entity(db, label="laptop", catalog_id="laptop", last_location="NOD2", last_seen_at=ts(6),
                           snapshot_uri=FRAME)
    make_sighting(db, pictured, seen_at=ts(5), location_id="NOD2", window_id=w)
    items = {i["entity_id"]: i for i in ok(client.get("/items"))["items"]}
    assert items[private]["snapshot_withheld_reason"] == "private_window"
    assert items[plain]["snapshot_withheld_reason"] is None
    assert items[pictured]["snapshot_withheld_reason"] is None
    assert ok(client.get(f"/items/{private}"))["snapshot_withheld_reason"] == "private_window"


def test_ac_api_44_review_subject_in_a_no_capture_zone_says_private_window(client, db):
    e = make_entity(db, label="laptop", catalog_id="laptop", last_location="NOD2S5", last_seen_at=ts(5),
                    snapshot_uri=FRAME)
    rid = make_review(db, entity_id=e, kind="low_confidence", location_id="LAB1", image_uri=FRAME)
    review = ok(client.get(f"/review/{rid}"))
    assert review["subject"]["thumbnail_url"] is None
    assert review["subject"]["snapshot_withheld_reason"] == "private_window"


# ------------------------------------------------------------------ review round
def test_review_withheld_crop_is_deleted_from_the_object_store(client, db, s3):
    s3.put_object(Bucket=BUCKET, Key="crops/0001-a.jpg", Body=b"\xff\xd8face")
    s3.put_object(Bucket=BUCKET, Key="frames/cam-lab1-fwd/0001.jpg", Body=b"\xff\xd8frame")
    _entity(client, payload(catalog_id=None, person_boxes=[LIVE_PERSON]))
    keys = {o["Key"] for o in s3.list_objects_v2(Bucket=BUCKET).get("Contents", [])}
    assert "crops/0001-a.jpg" not in keys and "frames/cam-lab1-fwd/0001.jpg" in keys
    assert client.get("/media/crops/0001-a.jpg").status_code == 404


def test_review_kept_crop_is_not_deleted(client, db, s3):
    s3.put_object(Bucket=BUCKET, Key="crops/0001-a.jpg", Body=b"\xff\xd8laptop")
    _entity(client, payload(bbox=CLEAR, person_boxes=[LIVE_PERSON]))
    keys = {o["Key"] for o in s3.list_objects_v2(Bucket=BUCKET).get("Contents", [])}
    assert "crops/0001-a.jpg" in keys


def test_review_face_kind_applies_the_whole_box_head_zone(client, db):
    face, crop = [0.4, 0.1, 0.5, 0.2], [0.2, 0.17, 0.7, 0.6]
    eid = _entity(client, payload(bbox=crop, person_boxes=[face], review_image=None))
    assert _state(db, eid)["exemplars"] != []  # as a person box: not an overlap
    body = payload(bbox=crop, person_boxes=[face], review_image=None)
    body["assignments"][0]["sighting"]["person_box_kinds"] = ["face"]
    body["window"]["t_start"] = iso(11)
    body["window"]["t_end"] = iso(11.1)
    eid2 = _entity(client, body)
    assert _state(db, eid2)["exemplars"] == []


@pytest.mark.parametrize("kinds", [["face", "person"], ["elbow"]])
def test_review_bad_person_box_kinds_are_422(client, db, kinds):
    body = payload(person_boxes=[LIVE_PERSON])
    body["assignments"][0]["sighting"]["person_box_kinds"] = kinds
    assert _ingest(client, body).status_code == 422


def test_review_inverted_person_box_is_422(client, db):
    assert _ingest(client, payload(person_boxes=[[0.5, 0.9, 0.3, 0.3]])).status_code == 422


@pytest.mark.parametrize("sighting_loc", ["LAB1S2", "LAB1"])
def test_review_person_moved_within_its_module_reads_as_at(client, db, sighting_loc):
    """A person 'moved' from LAB1 (no destination) sighted in a zone of LAB1 did not leave LAB1."""
    p = make_entity(db, label="person", kind="person", catalog_id="crew-member", description="blue shirt",
                    last_location="LAB1", last_seen_at=ts(12))
    make_sighting(db, p, seen_at=ts(10), location_id=sighting_loc)
    make_event(db, p, event_type="moved", occurred_at=ts(10), from_location="LAB1")
    (ev,) = ok(client.get("/events", params={"entity_id": p}))
    assert ev["location_role"] == "at"


def test_review_person_moved_from_the_segment_into_a_module_reads_as_at(client, db):
    p = make_entity(db, label="person", kind="person", catalog_id="crew-member", description="blue shirt",
                    last_location="NOD1", last_seen_at=ts(12))
    make_sighting(db, p, seen_at=ts(10), location_id="NOD1")
    make_event(db, p, event_type="moved", occurred_at=ts(10), from_location="USOS")
    (ev,) = ok(client.get("/events", params={"entity_id": p}))
    assert ev["location_role"] == "at"
