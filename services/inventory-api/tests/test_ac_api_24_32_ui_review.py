"""AC-API-24..32: contract additions requested by the Playwright UI review (docs/ui-review/REPORT.md).

UI-03 rich/suggested duplicate candidates, UI-08 image_bbox, UI-11 review filters, UI-13 event context,
UI-04 last_handled, UI-09 location provenance (operator edits do not fake a sighting), UI-12 history collapse.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest

from tests.conftest import (
    BUCKET,
    CONTRACTS_DB,
    PNG_BYTES,
    audit_rows,
    iso,
    make_entity,
    make_event,
    make_exemplar,
    make_review,
    make_sighting,
    ok,
    parse_dt,
    ts,
    vec,
)
from tests.test_ac_api_18_19_23_ingest import CROP, FRAME, payload

SNAP = f"s3://{BUCKET}/items/snap.jpg"
CANDIDATE_KEYS = {
    "entity_id", "label", "catalog_id", "status", "last_location", "last_location_name", "last_seen_at",
    "confidence", "thumbnail_url", "sighting_count", "similarity",
}
SUBJECT_KEYS = CANDIDATE_KEYS - {"similarity"}


def _ingest(client, body):
    return client.post("/ingest/window", json=body)


def _review(client, rid):
    return ok(client.get(f"/review/{rid}"))


def _dup_review(db, subject, candidate_ids=(), **kw):
    return make_review(db, entity_id=subject, kind="possible_duplicate", candidate_ids=candidate_ids, **kw)


# ====================================================================== AC-API-24 rich candidates + subject
def test_ac_api_24_stored_candidates_are_rich(client, db):
    subject = make_entity(db, label="camera", catalog_id="camera-dslr", last_location="LAB1S2", last_seen_at=ts(3))
    c1 = make_entity(db, label="camera", catalog_id="camera-dslr", status="confirmed", last_location="NOD1D1",
                     last_seen_at=ts(1), confidence=0.75, snapshot_uri=SNAP)
    c2 = make_entity(db, label="camera", catalog_id="camera-dslr", last_location="LAB1P3", last_seen_at=ts(2))
    for i in range(3):
        make_sighting(db, c1, seen_at=ts(i), location_id="NOD1D1")
    make_exemplar(db, subject, vec((0, 1.0)), quality=0.9)
    make_exemplar(db, c1, vec((0, 1.0), (1, 1.0)))
    rid = _dup_review(db, subject, [c1, c2])
    r = _review(client, rid)
    assert r["candidate_ids"] == [c1, c2]
    assert r["candidates_source"] == "stored"
    cands = {c["entity_id"]: c for c in r["candidates"]}
    assert set(cands) == {c1, c2}
    for c in cands.values():
        assert CANDIDATE_KEYS <= set(c)
        assert "kind" in c  # still a superset of Item (backward compatible)
    a = cands[c1]
    assert a["last_location_name"] == "Node 1 deck bay 1"
    assert a["sighting_count"] == 3
    assert a["thumbnail_url"] == "/media/items/snap.jpg"
    assert a["similarity"] == pytest.approx(0.7071, abs=1e-3)
    assert parse_dt(a["last_seen_at"]) == ts(1)
    assert cands[c2]["similarity"] is None  # c2 has no exemplar
    assert cands[c2]["sighting_count"] == 0


def test_ac_api_24_subject_describes_the_review_entity(client, db):
    subject = make_entity(db, label="headset", catalog_id="headset", last_location="LAB1S2", last_seen_at=ts(2),
                          confidence=0.6, snapshot_uri=SNAP)
    make_sighting(db, subject, seen_at=ts(2), location_id="LAB1S2")
    rid = make_review(db, entity_id=subject, kind="unknown_object")
    r = _review(client, rid)
    s = r["subject"]
    assert SUBJECT_KEYS <= set(s)
    assert s["entity_id"] == subject
    assert s["label"] == "headset"
    assert s["last_location_name"] == "Lab starboard rack 2"
    assert s["sighting_count"] == 1
    assert s["thumbnail_url"] == "/media/items/snap.jpg"
    assert s["confidence"] == pytest.approx(0.6)
    assert r["candidates"] == [] and r["candidates_source"] == "none"


def test_ac_api_24_subject_null_without_entity(client, db):
    rid = make_review(db, entity_id=None, kind="unknown_object")
    r = _review(client, rid)
    assert r["subject"] is None
    assert r["candidates"] == []


def test_ac_api_24_list_and_detail_have_the_same_shape(client, db):
    subject = make_entity(db, label="laptop", catalog_id="laptop", last_location="LAB1S2")
    make_entity(db, label="laptop", catalog_id="laptop", last_location="LAB1P3")
    rid = _dup_review(db, subject)
    listed = ok(client.get("/review"))
    assert len(listed) == 1
    assert listed[0] == _review(client, rid)


# ====================================================================== AC-API-25 server-side duplicate candidates
def test_ac_api_25_fills_live_same_catalog_entities(client, db):
    subject = make_entity(db, label="camera", catalog_id="camera-dslr", last_location="LAB1S2")
    live = [make_entity(db, label="camera", catalog_id="camera-dslr", status=s, last_location="NOD1D1")
            for s in ("confirmed", "provisional", "unknown")]
    target = live[0]
    make_entity(db, label="camera", catalog_id="camera-dslr", status="merged", merged_into=target)
    make_entity(db, label="camera", catalog_id="camera-dslr", status="dismissed")
    make_entity(db, label="camera", catalog_id="camcorder")  # other class
    make_entity(db, label="camera", catalog_id="camera-dslr", kind="person")  # other kind (cannot be merged)
    rid = _dup_review(db, subject)
    r = _review(client, rid)
    assert r["candidate_ids"] == []
    assert r["candidates_source"] == "suggested"
    assert {c["entity_id"] for c in r["candidates"]} == set(live)
    assert all(CANDIDATE_KEYS <= set(c) for c in r["candidates"])


def test_ac_api_25_ranked_by_similarity_to_best_exemplar(client, db):
    subject = make_entity(db, label="headset", catalog_id="headset")
    make_exemplar(db, subject, vec((5, 1.0)), quality=0.1)  # low quality: not the "best" exemplar
    make_exemplar(db, subject, vec((0, 1.0)), quality=0.9)
    close = make_entity(db, label="headset", catalog_id="headset", last_seen_at=ts(1))
    far = make_entity(db, label="headset", catalog_id="headset", last_seen_at=ts(9))
    none = make_entity(db, label="headset", catalog_id="headset", last_seen_at=ts(20))
    make_exemplar(db, close, vec((0, 1.0), (1, 0.1)))
    make_exemplar(db, far, vec((0, 1.0), (1, 2.0)))
    make_exemplar(db, far, vec((5, 1.0)))  # identical to the low-quality subject exemplar: must not count
    rid = _dup_review(db, subject)
    cands = _review(client, rid)["candidates"]
    assert [c["entity_id"] for c in cands] == [close, far, none]
    assert cands[0]["similarity"] > cands[1]["similarity"] > 0
    assert cands[2]["similarity"] is None


def test_ac_api_25_without_exemplars_same_module_first_then_recent(client, db):
    subject = make_entity(db, label="laptop", catalog_id="laptop", last_location="LAB1S2")
    other_old = make_entity(db, label="laptop", catalog_id="laptop", last_location="NOD1D1", last_seen_at=ts(1))
    same_old = make_entity(db, label="laptop", catalog_id="laptop", last_location="LAB1P3", last_seen_at=ts(2))
    other_new = make_entity(db, label="laptop", catalog_id="laptop", last_location="NOD2D4", last_seen_at=ts(9))
    same_new = make_entity(db, label="laptop", catalog_id="laptop", last_location="LAB1", last_seen_at=ts(5))
    never = make_entity(db, label="laptop", catalog_id="laptop", last_location="NOD1")
    rid = _dup_review(db, subject)
    got = [c["entity_id"] for c in _review(client, rid)["candidates"]]
    assert got == [same_new, same_old, other_new, other_old, never]


def test_ac_api_25_at_most_eight(client, db):
    subject = make_entity(db, label="tablet", catalog_id="tablet")
    for i in range(11):
        make_entity(db, label="tablet", catalog_id="tablet", last_seen_at=ts(i))
    rid = _dup_review(db, subject)
    assert len(_review(client, rid)["candidates"]) == 8


def test_ac_api_25_unknown_class_uses_normalised_label_key(client, db):
    subject = make_entity(db, label="Space Pen", status="unknown")
    a = make_entity(db, label="space-pen")
    b = make_entity(db, label="SPACEPEN", status="confirmed")
    make_entity(db, label="pen")
    make_entity(db, label="space pen", status="dismissed")
    rid = _dup_review(db, subject)
    assert {c["entity_id"] for c in _review(client, rid)["candidates"]} == {a, b}


def test_ac_api_25_only_for_possible_duplicate_with_no_stored_candidates(client, db):
    subject = make_entity(db, label="drill", catalog_id="drill-pistol")
    other = make_entity(db, label="drill", catalog_id="drill-pistol")
    third = make_entity(db, label="drill", catalog_id="drill-pistol")
    unknown = make_review(db, entity_id=subject, kind="unknown_object")
    low = make_review(db, entity_id=subject, kind="low_confidence")
    stored = _dup_review(db, subject, [other])
    assert _review(client, unknown)["candidates"] == []
    assert _review(client, low)["candidates"] == []
    r = _review(client, stored)
    assert [c["entity_id"] for c in r["candidates"]] == [other]
    assert third not in {c["entity_id"] for c in r["candidates"]}


def test_ac_api_25_list_fills_many_reviews(client, db):
    subjects = [make_entity(db, label="wrench", catalog_id="wrench", last_seen_at=ts(i)) for i in range(4)]
    for s in subjects:
        _dup_review(db, s)
    for r in ok(client.get("/review")):
        ids = {c["entity_id"] for c in r["candidates"]}
        assert ids == set(subjects) - {r["entity_id"]}


# ====================================================================== AC-API-26 image_bbox
def _dup_payload(image_uri):
    body = payload(events=[], decisions=[])
    body["reviews"][0]["image_uri"] = image_uri
    body["reviews"][0]["kind"] = "possible_duplicate"
    return body


def test_ac_api_26_image_bbox_from_the_frame_sighting(client, db):
    assert _ingest(client, _dup_payload(FRAME)).status_code == 201
    r = ok(client.get("/review"))[0]
    assert r["image_url"].endswith("frame.jpg") or r["image_url"].startswith("/media/")
    assert r["image_bbox"] == pytest.approx([0.1, 0.2, 0.3, 0.4], abs=1e-6)


def test_ac_api_26_image_bbox_null_for_a_crop(client, db):
    assert _ingest(client, _dup_payload(CROP)).status_code == 201
    assert ok(client.get("/review"))[0]["image_bbox"] is None


def test_ac_api_26_image_bbox_null_when_not_normalised(client, db):
    e = make_entity(db, label="drill")
    db.execute(
        "INSERT INTO sighting (entity_id, location_id, seen_at, bbox, confidence, frame_uri) "
        "VALUES (%s, 'LAB1S2', %s, %s, 0.9, %s)",
        (e, ts(1), [10.0, 20.0, 300.0, 400.0], FRAME),
    )
    rid = make_review(db, entity_id=e, image_uri=FRAME, observed_at=ts(1))
    assert _review(client, rid)["image_bbox"] is None


def test_ac_api_26_image_bbox_picks_the_sighting_nearest_observed_at(client, db):
    e = make_entity(db, label="drill")
    for minute, box in ((1, [0.0, 0.0, 0.5, 0.5]), (5, [0.5, 0.5, 1.0, 1.0])):
        db.execute(
            "INSERT INTO sighting (entity_id, location_id, seen_at, bbox, confidence, frame_uri) "
            "VALUES (%s, 'LAB1S2', %s, %s, 0.9, %s)",
            (e, ts(minute), box, FRAME),
        )
    rid = make_review(db, entity_id=e, image_uri=FRAME, observed_at=ts(5))
    assert _review(client, rid)["image_bbox"] == pytest.approx([0.5, 0.5, 1.0, 1.0])


# ====================================================================== AC-API-27 privacy
@pytest.mark.parametrize("private_loc", ["NOD2S5", "NOD2D5", "NOD2P5", "NOD2O5", "NOD3F4"])
def test_ac_api_27_no_thumbnails_from_no_capture_zones(client, db, private_loc):
    subject = make_entity(db, label="towel bag", catalog_id="m-bag", last_location=private_loc, snapshot_uri=SNAP)
    private = make_entity(db, label="m-bag", catalog_id="m-bag", last_location=private_loc, snapshot_uri=SNAP)
    public = make_entity(db, label="m-bag", catalog_id="m-bag", last_location="PMM1D2", snapshot_uri=SNAP)
    rid = _dup_review(db, subject, location_id="PMM1D2")
    stored = _dup_review(db, subject, [private], location_id="PMM1D2")
    r = _review(client, rid)
    assert r["subject"]["thumbnail_url"] is None and r["subject"]["snapshot_url"] is None
    cands = {c["entity_id"]: c for c in r["candidates"]}
    assert cands[private]["thumbnail_url"] is None and cands[private]["snapshot_url"] is None
    assert cands[public]["thumbnail_url"] == "/media/items/snap.jpg"
    assert _review(client, stored)["candidates"][0]["thumbnail_url"] is None


def test_ac_api_27_legacy_review_stored_in_no_capture_zone_has_no_image(client, db):
    e = make_entity(db, label="thing")
    rid = make_review(db, entity_id=e, image_uri=FRAME, location_id="NOD2D5")
    r = _review(client, rid)
    assert r["image_url"] is None and r["image_bbox"] is None


# ====================================================================== AC-API-28 review filters + detail
def test_ac_api_28_location_filter_follows_hierarchy(client, db):
    e = make_entity(db, label="u")
    lab_zone = make_review(db, entity_id=e, location_id="LAB1S2")
    lab = make_review(db, entity_id=e, location_id="LAB1")
    make_review(db, entity_id=e, location_id="NOD1D1")
    make_review(db, entity_id=e, location_id=None)
    got = {x["review_id"] for x in ok(client.get("/review", params={"location": "LAB1"}))}
    assert got == {lab_zone, lab}
    assert ok(client.get("/review", params={"location": "LAB"})) == []  # never a string prefix


def test_ac_api_28_q_matches_description_and_label(client, db):
    drill = make_entity(db, label="Pistol Drill")
    other = make_entity(db, label="bag")
    by_label = make_review(db, entity_id=drill, description="something black")
    by_desc = make_review(db, entity_id=other, description="looks like a DRILL bit case")
    make_review(db, entity_id=other, description="white bag")
    got = {x["review_id"] for x in ok(client.get("/review", params={"q": "drill"}))}
    assert got == {by_label, by_desc}


def test_ac_api_28_q_wildcards_are_literal(client, db):
    e = make_entity(db, label="x")
    pct = make_review(db, entity_id=e, description="100% full")
    make_review(db, entity_id=e, description="100 full")
    assert [x["review_id"] for x in ok(client.get("/review", params={"q": "100%"}))] == [pct]


@pytest.mark.parametrize("param", ["q", "location"])
def test_ac_api_28_filters_are_not_injectable(client, db, param):
    e = make_entity(db, label="x")
    make_review(db, entity_id=e)
    evil = "'; DROP TABLE review_item; --"
    r = client.get("/review", params={param: evil})
    assert r.status_code == 200 and r.json() == []
    assert db.execute("SELECT count(*) FROM review_item").fetchone()[0] == 1


def test_ac_api_28_sort_order_limit_offset_and_total_header(client, db):
    e = make_entity(db, label="x")
    # created order a<b<c, observed order c<a<b
    a = make_review(db, entity_id=e, created_at=ts(1), observed_at=ts(5))
    b = make_review(db, entity_id=e, created_at=ts(2), observed_at=ts(9))
    c = make_review(db, entity_id=e, created_at=ts(3), observed_at=ts(1))
    r = client.get("/review")
    assert [x["review_id"] for x in r.json()] == [c, b, a]
    assert r.headers["X-Total-Count"] == "3"
    assert [x["review_id"] for x in ok(client.get("/review", params={"sort": "observed_at"}))] == [b, a, c]
    assert [x["review_id"] for x in ok(client.get("/review", params={"sort": "observed_at", "order": "asc"}))] == [
        c, a, b]
    assert [x["review_id"] for x in ok(client.get("/review", params={"order": "asc"}))] == [a, b, c]
    page = client.get("/review", params={"limit": 1, "offset": 1})
    assert [x["review_id"] for x in page.json()] == [b]
    assert page.headers["X-Total-Count"] == "3"


def test_ac_api_28_total_header_counts_filtered_matches(client, db):
    e = make_entity(db, label="x")
    for _ in range(3):
        make_review(db, entity_id=e, kind="possible_duplicate")
    make_review(db, entity_id=e, kind="unknown_object")
    r = client.get("/review", params={"kind": "possible_duplicate", "limit": 2})
    assert len(r.json()) == 2 and r.headers["X-Total-Count"] == "3"


@pytest.mark.parametrize("params", [{"sort": "label"}, {"order": "up"}, {"offset": -1}, {"limit": 0}])
def test_ac_api_28_invalid_filter_values_are_422(client, db, params):
    assert client.get("/review", params=params).status_code == 422


def test_ac_api_28_get_review_by_id_404_and_422(client, db):
    assert client.get(f"/review/{uuid.uuid4()}").status_code == 404
    assert client.get("/review/not-a-uuid").status_code == 422


def test_ac_api_28_resolved_review_is_readable_by_id(client, db):
    e = make_entity(db, label="x")
    rid = make_review(db, entity_id=e, status="resolved")
    assert _review(client, rid)["status"] == "resolved"


# ====================================================================== AC-API-29 events
def test_ac_api_29_events_carry_entity_and_location_names(client, db):
    e = make_entity(db, label="laptop", catalog_id="laptop")
    make_event(db, e, event_type="moved", occurred_at=ts(2), from_location="LAB1S2", to_location="NOD1D1")
    make_event(db, e, event_type="used", occurred_at=ts(1))
    evs = ok(client.get("/events"))
    moved, used = evs
    assert moved["entity_label"] == "laptop" and moved["entity_catalog_id"] == "laptop"
    assert moved["from_location_name"] == "Lab starboard rack 2"
    assert moved["to_location_name"] == "Node 1 deck bay 1"
    assert used["from_location_name"] is None and used["to_location_name"] is None
    detail_ev = ok(client.get(f"/items/{e}"))["events"][0]
    assert detail_ev["entity_label"] == "laptop" and detail_ev["to_location_name"] == "Node 1 deck bay 1"


def test_ac_api_29_types_filter(client, db):
    e = make_entity(db, label="drill")
    for i, t in enumerate(("moved", "used", "seen", "handed_over")):
        make_event(db, e, event_type=t, occurred_at=ts(i))
    got = sorted(x["event_type"] for x in ok(client.get("/events", params={"types": "moved, used,handed_over"})))
    assert got == ["handed_over", "moved", "used"]
    both = ok(client.get("/events", params={"types": "moved,used", "type": "used"}))
    assert [x["event_type"] for x in both] == ["used"]
    assert client.get("/events", params={"types": "moved,bogus"}).status_code == 422
    assert client.get("/events", params={"types": "moved'; DROP TABLE event; --"}).status_code == 422


# ====================================================================== AC-API-30 last_handled
def test_ac_api_30_last_handled_latest_handling_event(client, db):
    e = make_entity(db, label="laptop", last_seen_at=ts(1))
    make_event(db, e, event_type="added", occurred_at=ts(0), source="operator", actor_desc="amy")
    make_event(db, e, event_type="moved", occurred_at=ts(1), actor_desc="crew, green shirt")
    make_event(db, e, event_type="used", occurred_at=ts(3), actor_desc="Blue shirt, black shorts, headset")
    make_event(db, e, event_type="seen", occurred_at=ts(9))
    expected = {"event_type": "used", "actor_desc": "Blue shirt, black shorts, headset"}
    for item in (ok(client.get(f"/items/{e}")), ok(client.get("/items"))["items"][0]):
        lh = item["last_handled"]
        assert {k: lh[k] for k in expected} == expected
        assert parse_dt(lh["occurred_at"]) == ts(3)


def test_ac_api_30_operator_added_is_ignored_but_pipeline_added_counts(client, db):
    op = make_entity(db, label="a")
    make_event(db, op, event_type="added", occurred_at=ts(5), source="operator", actor_desc="amy")
    pl = make_entity(db, label="b")
    make_event(db, pl, event_type="added", occurred_at=ts(5), source="pipeline", actor_desc="crew")
    assert ok(client.get(f"/items/{op}"))["last_handled"] is None
    assert ok(client.get(f"/items/{pl}"))["last_handled"]["event_type"] == "added"


def test_ac_api_30_new_operator_item_has_no_last_handled(client, db):
    r = client.post("/items", data={"label": "pen", "location_id": "LAB1S2", "operator": "amy"},
                    files={"image": ("p.png", PNG_BYTES, "image/png")})
    assert ok(r, 201)["last_handled"] is None


# ====================================================================== AC-API-31 location provenance
def _entity(db, e):
    return db.execute(
        "SELECT last_location, last_seen_at, location_set_at, location_set_by FROM entity WHERE entity_id=%s", (e,)
    ).fetchone()


def test_ac_api_31_operator_move_does_not_touch_last_seen(client, db):
    e = make_entity(db, label="drill", last_location="LAB1S2", last_seen_at=ts(0))
    before = datetime.now(UTC)
    item = ok(client.patch(f"/items/{e}", json={"last_location": "NOD1D1", "operator": "amy"}))
    loc, seen, set_at, set_by = _entity(db, e)
    assert (loc, seen, set_by) == ("NOD1D1", ts(0), "amy")
    assert before - timedelta(seconds=5) <= set_at <= datetime.now(UTC) + timedelta(seconds=5)
    assert parse_dt(item["last_seen_at"]) == ts(0)
    assert item["location_set_by"] == "amy" and parse_dt(item["location_set_at"]) == set_at


def test_ac_api_31_patch_without_move_keeps_provenance(client, db):
    e = make_entity(db, label="drill", last_location="LAB1S2", last_seen_at=ts(0))
    db.execute("UPDATE entity SET location_set_at=%s, location_set_by='pipeline' WHERE entity_id=%s", (ts(0), e))
    ok(client.patch(f"/items/{e}", json={"last_location": "LAB1S2", "label": "big drill", "operator": "amy"}))
    assert _entity(db, e) == ("LAB1S2", ts(0), ts(0), "pipeline")


def test_ac_api_31_late_sighting_refreshes_last_seen_but_not_location(client, db):
    e = make_entity(db, label="drill", last_location="LAB1S2", last_seen_at=ts(0))
    ok(client.patch(f"/items/{e}", json={"last_location": "NOD1D1", "operator": "amy"}))
    body = payload(events=[], reviews=[], decisions=[])
    body["assignments"][0] = {"entity_id": e, "sighting": {"location_id": "LAB1S2", "seen_at": iso(1),
                                                           "confidence": 0.9}}
    assert _ingest(client, body).status_code == 201
    loc, seen, _, set_by = _entity(db, e)
    assert (loc, seen, set_by) == ("NOD1D1", ts(1), "amy")  # the camera did see it at ts(1); the move stands


def test_ac_api_31_sighting_after_the_operator_move_moves_the_item(client, db):
    e = make_entity(db, label="drill", last_location="LAB1S2", last_seen_at=ts(0))
    ok(client.patch(f"/items/{e}", json={"last_location": "NOD1D1", "operator": "amy"}))
    later = datetime.now(UTC) + timedelta(minutes=5)
    body = payload(events=[], reviews=[], decisions=[])
    body["window"]["t_start"] = body["window"]["t_end"] = later.isoformat()
    body["assignments"][0] = {"entity_id": e, "sighting": {"location_id": "COL1A1", "seen_at": later.isoformat(),
                                                           "confidence": 0.9}}
    assert _ingest(client, body).status_code == 201
    assert _entity(db, e) == ("COL1A1", later, later, "pipeline")


def test_ac_api_31_ingest_new_entity_sets_pipeline_provenance(client, db):
    eid = ok(_ingest(client, payload()), 201)["entities"]["o1"]
    assert _entity(db, eid) == ("LAB1S2", ts(10), ts(10), "pipeline")
    item = ok(client.get(f"/items/{eid}"))
    assert item["location_set_by"] == "pipeline" and parse_dt(item["location_set_at"]) == ts(10)


def test_ac_api_31_operator_add_sets_operator_provenance(client, db):
    r = client.post("/items", data={"label": "pen", "location_id": "LAB1S2", "operator": "amy"},
                    files={"image": ("p.png", PNG_BYTES, "image/png")})
    item = ok(r, 201)
    assert item["location_set_by"] == "amy" and item["location_set_at"] is not None


def test_ac_api_31_ingest_without_provenance_falls_back_to_last_seen(client, db):
    e = make_entity(db, label="drill", last_location="LAB1S2", last_seen_at=ts(5))
    body = payload(events=[], reviews=[], decisions=[])
    body["assignments"][0] = {"entity_id": e, "sighting": {"location_id": "NOD1D1", "seen_at": iso(3),
                                                           "confidence": 0.9}}
    assert _ingest(client, body).status_code == 201
    assert _entity(db, e)[:2] == ("LAB1S2", ts(5))


def test_ac_api_31_merge_takes_the_newer_location_by_provenance(client, db):
    target = make_entity(db, label="drill", last_location="LAB1S2", last_seen_at=ts(1))
    source = make_entity(db, label="drill dup", last_location="LAB1P3", last_seen_at=ts(5))
    # the operator moved the target after the source's last sighting
    db.execute("UPDATE entity SET last_location='NOD1D1', location_set_at=%s, location_set_by='amy' "
               "WHERE entity_id=%s", (ts(9), target))
    ok(client.post(f"/items/{source}/merge", json={"into_entity_id": target, "operator": "amy"}))
    assert _entity(db, target) == ("NOD1D1", ts(5), ts(9), "amy")
    tp = audit_rows(db, "merge")[-1][3]["target_previous"]
    assert tp["location_set_at"] == ts(9).isoformat() and tp["location_set_by"] == "amy"


def test_ac_api_31_merge_newer_source_location_moves_provenance(client, db):
    target = make_entity(db, label="drill", last_location="LAB1S2", last_seen_at=ts(1))
    source = make_entity(db, label="drill dup", last_location="LAB1P3", last_seen_at=ts(5))
    db.execute("UPDATE entity SET location_set_at=%s, location_set_by='pipeline' WHERE entity_id=%s", (ts(5), source))
    ok(client.post(f"/items/{source}/merge", json={"into_entity_id": target, "operator": "amy"}))
    assert _entity(db, target) == ("LAB1P3", ts(5), ts(5), "pipeline")


MIGRATION_003 = CONTRACTS_DB / "003_location_provenance.sql"


def test_ac_api_31_migration_is_idempotent_and_backfills(db):
    plain = make_entity(db, label="a", last_location="LAB1S2", last_seen_at=ts(4))
    # An item moved by an operator under the old GREATEST rule: last_seen_at was bumped to the move time.
    faked = make_entity(db, label="b", last_location="NOD1D1", last_seen_at=ts(30))
    make_sighting(db, faked, seen_at=ts(2), location_id="LAB1S2")
    make_sighting(db, faked, seen_at=ts(7), location_id="LAB1S2")
    make_event(db, faked, event_type="moved", occurred_at=ts(30), source="operator", actor_desc="ui-review",
               from_location="LAB1S2", to_location="NOD1D1")
    # Pipeline move at the same instant as last_seen: not a fake sighting.
    piped = make_entity(db, label="c", last_location="NOD1D1", last_seen_at=ts(8))
    make_sighting(db, piped, seen_at=ts(8), location_id="NOD1D1")
    make_event(db, piped, event_type="moved", occurred_at=ts(8), source="pipeline")
    db.execute("UPDATE entity SET location_set_at=NULL, location_set_by=NULL")
    for _ in range(2):
        db.execute(MIGRATION_003.read_text())
    assert _entity(db, plain) == ("LAB1S2", ts(4), ts(4), "pipeline")
    assert _entity(db, faked) == ("NOD1D1", ts(7), ts(30), "ui-review")
    assert _entity(db, piped) == ("NOD1D1", ts(8), ts(8), "pipeline")


def test_ac_api_31_migration_backfills_operator_created_items(db):
    e = make_entity(db, label="pen", last_location="LAB1S2", last_seen_at=ts(3), created_by="operator")
    db.execute("UPDATE entity SET location_set_at=NULL, location_set_by=NULL")
    db.execute(MIGRATION_003.read_text())
    assert _entity(db, e) == ("LAB1S2", ts(3), ts(3), "operator")


# ====================================================================== AC-API-32 history collapse
def _pipeline_audit(db, e, at, **detail):
    d = {"window_id": str(uuid.uuid4()), "created_entity_ids": [], "updated_entity_ids": [e], "event_ids": [],
         "review_ids": []}
    d.update(detail)
    db.execute("INSERT INTO audit_log (op, entity_ids, actor, detail, at) VALUES ('update', ARRAY[%s]::uuid[], "
               "'pipeline', %s::jsonb, %s)", (e, __import__("json").dumps(d), at))


def test_ac_api_32_collapses_consecutive_routine_pipeline_updates(client, db):
    e = make_entity(db, label="person", kind="person")
    for i in range(5):
        _pipeline_audit(db, e, ts(10 + i))
    make_sighting(db, e, seen_at=ts(5), location_id="LAB1S2")
    timeline = ok(client.get(f"/items/{e}/history"))
    assert [t["kind"] for t in timeline] == ["audit", "sighting"]
    folded = timeline[0]
    assert folded["count"] == 5
    assert parse_dt(folded["at"]) == ts(14) and parse_dt(folded["first_at"]) == ts(10)
    assert len(folded["audit_ids"]) == 5
    assert "5 pipeline updates" in folded["summary"]
    raw = ok(client.get(f"/items/{e}/history", params={"collapse": "false"}))
    assert [t["kind"] for t in raw] == ["audit"] * 5 + ["sighting"]


def test_ac_api_32_runs_are_split_by_other_entries(client, db):
    e = make_entity(db, label="drill")
    _pipeline_audit(db, e, ts(1))
    _pipeline_audit(db, e, ts(2))
    make_sighting(db, e, seen_at=ts(3), location_id="LAB1S2")
    _pipeline_audit(db, e, ts(4))
    _pipeline_audit(db, e, ts(5))
    _pipeline_audit(db, e, ts(6))
    timeline = ok(client.get(f"/items/{e}/history"))
    assert [(t["kind"], t.get("count")) for t in timeline] == [("audit", 3), ("sighting", None), ("audit", 2)]


def test_ac_api_32_meaningful_pipeline_audits_are_not_folded(client, db):
    e = make_entity(db, label="drill")
    ev = make_event(db, e, event_type="moved", occurred_at=ts(0))
    _pipeline_audit(db, e, ts(1), created_entity_ids=[e], updated_entity_ids=[])
    _pipeline_audit(db, e, ts(2), event_ids=[ev])
    _pipeline_audit(db, e, ts(3), event_ids=[str(uuid.uuid4())])  # another entity's event: routine for e
    _pipeline_audit(db, e, ts(4))
    timeline = ok(client.get(f"/items/{e}/history"))
    assert [(t["kind"], t.get("count")) for t in timeline] == [
        ("audit", 2), ("audit", 1), ("audit", 1), ("event", None)]


def test_ac_api_32_single_routine_audit_and_operator_audits_count_one(client, db):
    e = make_entity(db, label="drill", last_location="LAB1S2")
    _pipeline_audit(db, e, ts(1))
    ok(client.patch(f"/items/{e}", json={"last_location": "NOD1D1", "operator": "amy"}))
    timeline = ok(client.get(f"/items/{e}/history"))
    audits = [t for t in timeline if t["kind"] == "audit"]
    assert [a["count"] for a in audits] == [1, 1]
    assert "last_location: LAB1S2 -> NOD1D1" in audits[0]["summary"]
    assert audits[0]["actor"] == "amy"


def test_ac_api_32_invalid_collapse_value_is_422(client, db):
    e = make_entity(db, label="drill")
    assert client.get(f"/items/{e}/history", params={"collapse": "maybe"}).status_code == 422


# ====================================================================== adversarial self-review guards
def _count_queries(monkeypatch):
    import psycopg

    calls = {"n": 0}
    original = psycopg.Connection.execute

    def counting(self, *args, **kwargs):
        calls["n"] += 1
        return original(self, *args, **kwargs)

    monkeypatch.setattr(psycopg.Connection, "execute", counting)
    return calls


def test_ac_api_25_review_queue_query_count_does_not_grow_with_reviews(client, db, monkeypatch):
    """No N+1: the number of SQL statements for a page is the same for 1 and for 12 duplicate reviews."""

    def page_queries(n_reviews):
        db.execute("TRUNCATE review_item, entity_exemplar, sighting, event, entity CASCADE")
        for i in range(n_reviews):
            s = make_entity(db, label="wrench", catalog_id="wrench", last_location="LAB1S2", last_seen_at=ts(i))
            make_exemplar(db, s, vec((i, 1.0)))
            make_sighting(db, s, seen_at=ts(i), location_id="LAB1S2", frame_uri=FRAME)
            other = make_entity(db, label="wrench", catalog_id="wrench")
            _dup_review(db, s, image_uri=FRAME, observed_at=ts(i))
            _dup_review(db, s, [other], image_uri=FRAME, observed_at=ts(i))
        calls = _count_queries(monkeypatch)
        assert len(ok(client.get("/review"))) == 2 * n_reviews
        monkeypatch.undo()
        return calls["n"]

    one, six = page_queries(1), page_queries(6)
    assert one > 3  # the counter really sees the API's statements
    assert one == six


@pytest.mark.parametrize("params", [{"q": "a\x00b"}, {"location": "LAB1\x00"}])
def test_ac_api_28_nul_bytes_in_review_filters_are_422(client, db, params):
    assert client.get("/review", params=params).status_code == 422


def test_ac_api_29_nul_byte_in_types_is_422(client, db):
    assert client.get("/events", params={"types": "moved,\x00"}).status_code == 422


def test_ac_api_27_suggested_candidates_in_list_never_leak_private_snapshots(client, db):
    subject = make_entity(db, label="headset", catalog_id="headset", last_location="LAB1S2")
    for loc in ("NOD2S5", "NOD2D5", "NOD2P5", "NOD2O5", "NOD3F4"):
        make_entity(db, label="headset", catalog_id="headset", last_location=loc, snapshot_uri=SNAP)
    _dup_review(db, subject)
    cands = ok(client.get("/review"))[0]["candidates"]
    assert len(cands) == 5
    assert all(c["thumbnail_url"] is None and c["snapshot_url"] is None for c in cands)
