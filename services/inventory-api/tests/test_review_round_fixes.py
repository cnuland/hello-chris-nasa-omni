"""Review round 1: regression tests for the reviewer's findings (each one failed before its fix).

Grouped by finding; the AC tag in each test name says which acceptance criterion it strengthens.
"""

from __future__ import annotations

import copy
import threading
import time
import uuid

import psycopg
import pytest
from botocore.exceptions import ClientError, EndpointConnectionError
from fastapi.testclient import TestClient
from psycopg.rows import dict_row

from inventory_api.main import create_app
from inventory_api.merging import merge_entities
from inventory_api.storage import S3ObjectStore
from tests.conftest import (
    BUCKET,
    JPEG_BYTES,
    PNG_BYTES,
    audit_rows,
    iso,
    make_entity,
    make_event,
    make_exemplar,
    make_review,
    make_sighting,
    ok,
    table_counts,
    ts,
    vec,
)
from tests.test_ac_api_18_19_23_ingest import CROP, FRAME, payload

CQ = "NOD2S5"  # crew quarters (no_capture)
CQ_DECK = "NOD2D5"  # crew quarters deck (no_capture)


def _ingest(client, body):
    return client.post("/ingest/window", json=body)


def _cq_new_entity_body(**review):
    """A new entity 'a' sighted in crew quarters with exemplar crop CROP."""
    body = payload(events=[], reviews=[], decisions=[])
    body["assignments"][0]["ref"] = "a"
    body["assignments"][0]["sighting"]["location_id"] = CQ
    if review:
        rv = {"kind": "unknown_object", "image_uri": CROP, "description": "what is this", "observed_at": iso(10)}
        rv.update(review)
        body["reviews"] = [rv]
    return body


def _private_camera(db, camera_id="cam-cq", location_id=CQ):
    db.execute(
        "INSERT INTO camera (camera_id, location_id, description) VALUES (%s, %s, 'test cam in CQ')",
        (camera_id, location_id),
    )
    return camera_id


# ====================================================================== F1 (high): review privacy bypasses
def test_ac_api_19_review_with_public_location_but_entity_sighted_in_crew_quarters_rejected(client, db):
    before = table_counts(db)
    r = _ingest(client, _cq_new_entity_body(entity_ref="a", location_id="NOD2"))
    assert r.status_code == 422, r.text
    assert table_counts(db) == before


def test_ac_api_19_review_of_existing_entity_last_seen_in_no_capture_zone_rejected(client, db):
    e = make_entity(db, label="sleeping bag", last_location=CQ_DECK, last_seen_at=ts(1))
    body = payload(assignments=[], events=[], decisions=[])
    body["reviews"] = [
        {"kind": "low_confidence", "entity_id": e, "image_uri": CROP, "description": "bag?", "observed_at": iso(10)}
    ]
    before = table_counts(db)
    assert _ingest(client, body).status_code == 422
    assert table_counts(db) == before


def test_ac_api_19_review_with_no_location_or_entity_falls_back_to_private_camera_location(client, db):
    cam = _private_camera(db)
    body = payload(assignments=[], events=[], decisions=[])
    body["window"]["camera_id"] = cam
    body["reviews"] = [{"kind": "unknown_object", "image_uri": CROP, "description": "unknown", "observed_at": iso(10)}]
    before = table_counts(db)
    assert _ingest(client, body).status_code == 422
    assert table_counts(db) == before


def test_ac_api_19_review_reusing_an_image_dropped_for_privacy_rejected(client, db):
    # 'a' is in crew quarters (its frame and crop are dropped); a review about a different, public entity tries to
    # carry the crew-quarters crop as its image.
    body = _cq_new_entity_body()
    public = copy.deepcopy(body["assignments"][0])
    public.update(ref="b", exemplar=None)
    public["sighting"] = dict(public["sighting"], location_id="LAB1S2", frame_uri=f"s3://{BUCKET}/frames/ok.jpg")
    body["assignments"].append(public)
    body["reviews"] = [
        {
            "kind": "unknown_object",
            "entity_ref": "b",
            "location_id": "LAB1S2",
            "image_uri": CROP,
            "description": "x",
            "observed_at": iso(10),
        }
    ]
    assert _ingest(client, body).status_code == 422
    body["reviews"][0]["image_uri"] = FRAME  # the dropped frame is refused too
    assert _ingest(client, body).status_code == 422
    body["reviews"][0]["image_uri"] = f"s3://{BUCKET}/frames/ok.jpg"
    assert _ingest(client, body).status_code == 201


def test_ac_api_19_public_review_of_public_entity_still_accepted(client, db):
    e = make_entity(db, label="drill", last_location="LAB1S2", last_seen_at=ts(1))
    body = payload(assignments=[], events=[], decisions=[])
    body["reviews"] = [
        {"kind": "low_confidence", "entity_id": e, "image_uri": CROP, "description": "drill?", "observed_at": iso(10)}
    ]
    assert _ingest(client, body).status_code == 201


# ====================================================================== F2: event evidence + private exemplars
def test_ac_api_19_event_without_from_to_for_entity_sighted_in_crew_quarters_drops_evidence(client, db):
    body = _cq_new_entity_body()
    body["events"] = [
        {
            "event_type": "used",
            "entity_ref": "a",
            "occurred_at": iso(10),
            "confidence": 0.7,
            "evidence_uri": f"s3://{BUCKET}/frames/other.jpg",
        }
    ]
    r = _ingest(client, body)
    assert r.status_code == 201, r.text
    eid = r.json()["entities"]["a"]
    assert db.execute("SELECT evidence_uri FROM event WHERE entity_id=%s", (eid,)).fetchone()[0] is None


def test_ac_api_19_event_without_from_to_or_sighting_uses_camera_location(client, db):
    cam = _private_camera(db)
    e = make_entity(db, label="pen", last_location="LAB1S2", last_seen_at=ts(1))
    body = payload(assignments=[], reviews=[], decisions=[])
    body["window"]["camera_id"] = cam
    body["events"] = [
        {
            "event_type": "used",
            "entity_id": e,
            "occurred_at": iso(10),
            "confidence": 0.7,
            "evidence_uri": f"s3://{BUCKET}/frames/other.jpg",
        }
    ]
    assert _ingest(client, body).status_code == 201
    assert db.execute("SELECT evidence_uri FROM event WHERE entity_id=%s", (e,)).fetchone()[0] is None


def test_ac_api_19_sighting_without_location_on_private_camera_drops_frame(client, db):
    cam = _private_camera(db)
    body = payload(events=[], reviews=[], decisions=[])
    body["window"]["camera_id"] = cam
    body["assignments"][0]["sighting"]["location_id"] = None
    r = _ingest(client, body)
    assert r.status_code == 201, r.text
    eid = r.json()["entities"]["o1"]
    assert db.execute("SELECT frame_uri FROM sighting WHERE entity_id=%s", (eid,)).fetchone()[0] is None
    assert db.execute("SELECT snapshot_uri FROM entity WHERE entity_id=%s", (eid,)).fetchone()[0] is None


def test_ac_api_19_public_event_keeps_evidence(client, db):
    body = payload(reviews=[], decisions=[])
    body["events"] = [
        {"event_type": "used", "entity_ref": "o1", "occurred_at": iso(10), "confidence": 0.7, "evidence_uri": FRAME}
    ]
    r = _ingest(client, body)
    eid = ok(r, 201)["entities"]["o1"]
    assert db.execute("SELECT evidence_uri FROM event WHERE entity_id=%s", (eid,)).fetchone()[0] == FRAME


def test_ac_api_19_person_exemplar_from_no_capture_zone_not_stored(client, db):
    body = _cq_new_entity_body()
    body["assignments"][0]["new_entity"].update(kind="person", catalog_id=None, label="crew member")
    r = _ingest(client, body)
    eid = ok(r, 201)["entities"]["a"]
    assert db.execute("SELECT count(*) FROM entity_exemplar WHERE entity_id=%s", (eid,)).fetchone()[0] == 0


def test_ac_api_19_existing_person_exemplar_from_no_capture_zone_not_stored(client, db):
    p = make_entity(db, label="crew member", kind="person", last_location="LAB1S2", last_seen_at=ts(1))
    body = payload(events=[], reviews=[], decisions=[])
    body["assignments"][0] = {
        "entity_id": p,
        "sighting": {"location_id": CQ, "seen_at": iso(10), "confidence": 0.9},
        "exemplar": {"embedding": vec((3, 1.0)), "quality": 0.9, "crop_uri": CROP},
    }
    assert _ingest(client, body).status_code == 201
    assert db.execute("SELECT count(*) FROM entity_exemplar WHERE entity_id=%s", (p,)).fetchone()[0] == 0


def test_ac_api_19_object_exemplar_from_no_capture_zone_keeps_embedding_without_crop(client, db):
    r = _ingest(client, _cq_new_entity_body())
    eid = ok(r, 201)["entities"]["a"]
    rows = db.execute("SELECT crop_uri FROM entity_exemplar WHERE entity_id=%s", (eid,)).fetchall()
    assert rows == [(None,)]


# ====================================================================== F3: audit on update-only ingest
def test_ac_api_21_update_only_ingest_writes_pipeline_audit_row(client, db):
    e = make_entity(db, label="drill", last_location="LAB1P3", last_seen_at=ts(1))
    body = {
        "window": payload()["window"],
        "assignments": [{"entity_id": e, "sighting": {"location_id": "LAB1S2", "seen_at": iso(10), "confidence": 0.9}}],
        "stock_updates": [{"catalog_id": "food-pack", "location_id": "NOD1D1", "est_count": 4, "seen_at": iso(10)}],
    }
    before = len(audit_rows(db))
    r = _ingest(client, body)
    wid = ok(r, 201)["window_id"]
    rows = audit_rows(db)[before:]
    assert rows, "an ingest that only updates must still be audited"
    assert all(a[2] == "pipeline" for a in rows)
    upd = [a for a in rows if a[0] == "update"]
    assert len(upd) == 1
    assert e in [str(x) for x in upd[0][1]]
    detail = upd[0][3]
    assert detail["window_id"] == wid
    assert e in detail["updated_entity_ids"]
    assert {"catalog_id": "food-pack", "location_id": "NOD1D1"} in detail["stock_keys"]


# ====================================================================== F4: client-caused DB errors -> 4xx
@pytest.mark.parametrize(
    "path",
    [
        "/items?q=a%00b",
        "/items?location=a%00b",
        "/items?catalog_id=a%00b",
        "/search?q=a%00b",
        "/events?type=a%00b",
        "/stock?location=a%00b",
    ],
)
def test_ac_api_22_nul_in_query_param_is_422_not_500(client, path):
    r = client.get(path)
    assert r.status_code == 422, r.text


def test_ac_api_22_nul_in_patch_label_and_attributes_is_422(client, db):
    e = make_entity(db, label="x")
    assert client.patch(f"/items/{e}", json={"label": "a\u0000b"}).status_code == 422
    assert client.patch(f"/items/{e}", json={"attributes": {"k": "a\u0000b"}}).status_code == 422
    assert db.execute("SELECT label FROM entity WHERE entity_id=%s", (e,)).fetchone()[0] == "x"


def test_ac_api_22_nul_in_catalog_class_name_is_422(client, db):
    r = client.post("/catalog", json={"catalog_id": "nul-test", "class_name": "a\u0000b", "display_name": "X"})
    assert r.status_code == 422, r.text


def test_ac_api_22_nul_in_operator_upload_label_is_422(client, db):
    r = client.post(
        "/items",
        data={"label": "a\u0000b", "location_id": "LAB1S2"},
        files={"image": ("x.png", PNG_BYTES, "image/png")},
    )
    assert r.status_code == 422, r.text


@pytest.mark.parametrize("value", [1e6, -70000.0, 65520.0])
def test_ac_api_17_embedding_outside_halfvec_range_is_422(client, value):
    emb = vec((0, 1.0))
    emb[1] = value
    r = client.post("/ingest/candidates", json={"embedding": emb})
    assert r.status_code == 422, r.text


def test_ac_api_17_embedding_at_halfvec_max_is_accepted(client):
    emb = vec((0, 1.0))
    emb[1] = 65504.0
    assert client.post("/ingest/candidates", json={"embedding": emb}).status_code == 200


# ====================================================================== F5: ingest racing a merge
def test_ac_api_18_ingest_racing_a_merge_attaches_to_the_survivor(client, db, database_url):
    target = make_entity(db, label="drill", last_location="LAB1S2", last_seen_at=ts(1))
    source = make_entity(db, label="drill dup", last_location="LAB1P3", last_seen_at=ts(2))
    body = payload(events=[], reviews=[], decisions=[])
    body["assignments"][0] = {
        "entity_id": source,
        "sighting": {"location_id": "NOD1D1", "seen_at": iso(30), "confidence": 0.9},
    }
    result: dict = {}

    with psycopg.connect(database_url, row_factory=dict_row) as merger:
        merge_entities(merger, uuid.UUID(source), uuid.UUID(target), "amy")
        # The merge holds its row locks but has not committed; start the ingest now.
        t = threading.Thread(target=lambda: result.setdefault("r", _ingest(client, body)))
        t.start()
        deadline = time.monotonic() + 10
        waiting = 0
        while time.monotonic() < deadline:
            waiting = db.execute(
                "SELECT count(*) FROM pg_stat_activity WHERE datname = current_database() AND wait_event_type = 'Lock'"
            ).fetchone()[0]
            if waiting:
                break
            time.sleep(0.02)
        assert waiting, "the ingest should block on the merge's locks"
        merger.commit()
    t.join(15)
    assert result["r"].status_code == 201, result["r"].text
    assert db.execute("SELECT count(*) FROM sighting WHERE entity_id=%s", (source,)).fetchone()[0] == 0
    assert db.execute("SELECT count(*) FROM sighting WHERE entity_id=%s", (target,)).fetchone()[0] == 1
    row = db.execute("SELECT last_location, last_seen_at FROM entity WHERE entity_id=%s", (target,)).fetchone()
    assert row == ("NOD1D1", ts(30))
    assert db.execute("SELECT last_location FROM entity WHERE entity_id=%s", (source,)).fetchone()[0] == "LAB1P3"


# ====================================================================== F6: operator move vs late pipeline window
def test_ac_api_08_operator_move_not_overwritten_by_older_late_window(client, db):
    e = make_entity(db, label="drill", last_location="LAB1S2", last_seen_at=ts(0))
    ok(client.patch(f"/items/{e}", json={"last_location": "NOD1D1", "operator": "amy"}))
    body = payload(events=[], reviews=[], decisions=[])
    body["assignments"][0] = {
        "entity_id": e,
        "sighting": {"location_id": "LAB1S2", "seen_at": iso(1), "confidence": 0.9},
    }
    assert _ingest(client, body).status_code == 201
    assert db.execute("SELECT last_location FROM entity WHERE entity_id=%s", (e,)).fetchone()[0] == "NOD1D1"
    # The late sighting is still recorded as history.
    assert db.execute("SELECT count(*) FROM sighting WHERE entity_id=%s", (e,)).fetchone()[0] == 1


def test_ac_api_08_patch_without_location_change_keeps_last_seen(client, db):
    e = make_entity(db, label="drill", last_location="LAB1S2", last_seen_at=ts(0))
    ok(client.patch(f"/items/{e}", json={"label": "big drill"}))
    assert db.execute("SELECT last_seen_at FROM entity WHERE entity_id=%s", (e,)).fetchone()[0] == ts(0)


# ====================================================================== F7: merge consistency
def _merge(client, source, target, operator="amy"):
    return client.post(f"/items/{source}/merge", json={"into_entity_id": target, "operator": operator})


def test_ac_api_18_merge_enforces_exemplar_cap_and_records_dropped(client, db):
    target = make_entity(db, label="t", last_seen_at=ts(1))
    source = make_entity(db, label="s", last_seen_at=ts(2))
    for i in range(20):
        make_exemplar(db, target, vec((i, 1.0)), quality=0.50 + i / 100)  # 0.50..0.69
        make_exemplar(db, source, vec((100 + i, 1.0)), quality=0.10 + i / 25)  # 0.10..0.86
    assert _merge(client, source, target).status_code == 200
    qs = sorted(r[0] for r in db.execute("SELECT quality FROM entity_exemplar WHERE entity_id=%s", (target,)))
    assert len(qs) == 20
    detail = audit_rows(db, "merge")[-1][3]
    dropped = detail["dropped_exemplars"]
    assert len(dropped) == 20
    assert all({"exemplar_id", "entity_id", "embedding", "quality", "crop_uri"} <= set(d) for d in dropped)
    assert min(qs) >= max(d["quality"] for d in dropped) - 1e-6


def test_ac_api_09_merge_repoints_actor_and_parent_references(client, db):
    target = make_entity(db, label="crew A", kind="person")
    source = make_entity(db, label="crew A dup", kind="person")
    thing = make_entity(db, label="drill")
    ev = make_event(db, thing, occurred_at=ts(1))
    db.execute("UPDATE event SET actor_entity=%s WHERE event_id=%s", (source, ev))
    child = make_entity(db, label="bag content")
    bag_t = make_entity(db, label="bag")
    bag_s = make_entity(db, label="bag dup")
    db.execute("UPDATE entity SET parent_entity=%s WHERE entity_id=%s", (bag_s, child))
    assert _merge(client, source, target).status_code == 200
    assert str(db.execute("SELECT actor_entity FROM event WHERE event_id=%s", (ev,)).fetchone()[0]) == target
    assert _merge(client, bag_s, bag_t).status_code == 200
    assert str(db.execute("SELECT parent_entity FROM entity WHERE entity_id=%s", (child,)).fetchone()[0]) == bag_t
    details = [a[3] for a in audit_rows(db, "merge")]
    assert ev in details[0]["moved"]["actor_event_ids"]
    assert child in details[1]["moved"]["child_entity_ids"]


def test_ac_api_09_merge_repoints_open_reviews_and_candidates(client, db):
    target = make_entity(db, label="t")
    source = make_entity(db, label="s", needs_review=True, review_reason="unknown_object")
    other = make_entity(db, label="o")
    rv_own = make_review(db, entity_id=source)
    rv_cand = make_review(db, entity_id=other, kind="possible_duplicate", candidate_ids=[source, other])
    rv_closed = make_review(db, entity_id=source, status="resolved")
    assert _merge(client, source, target).status_code == 200
    assert str(db.execute("SELECT entity_id FROM review_item WHERE review_id=%s", (rv_own,)).fetchone()[0]) == target
    cands = db.execute("SELECT candidate_ids FROM review_item WHERE review_id=%s", (rv_cand,)).fetchone()[0]
    assert [str(c) for c in cands] == [target, other]
    assert str(db.execute("SELECT entity_id FROM review_item WHERE review_id=%s", (rv_closed,)).fetchone()[0]) == source
    assert db.execute("SELECT needs_review FROM entity WHERE entity_id=%s", (target,)).fetchone()[0] is True
    # The re-pointed review is resolvable now (it was a permanent 409 before).
    r = client.post(
        f"/review/{rv_own}/resolve", json={"action": "assign_catalog", "catalog_id": "drill-pistol", "operator": "amy"}
    )
    assert r.status_code == 200, r.text
    detail = audit_rows(db, "merge")[0][3]
    assert rv_own in detail["moved"]["review_ids"]
    assert rv_cand in detail["moved"]["candidate_review_ids"]


def test_ac_api_13_review_merge_into_does_not_repoint_the_review_being_resolved(client, db):
    target = make_entity(db, label="t")
    source = make_entity(db, label="s", needs_review=True)
    rv = make_review(db, entity_id=source, kind="possible_duplicate", candidate_ids=[target])
    r = client.post(
        f"/review/{rv}/resolve", json={"action": "merge_into", "target_entity_id": target, "operator": "amy"}
    )
    assert r.status_code == 200, r.text
    row = db.execute("SELECT entity_id, status FROM review_item WHERE review_id=%s", (rv,)).fetchone()
    assert str(row[0]) == source and row[1] == "resolved"


def test_ac_api_09_merge_into_dismissed_target_is_409(client, db):
    target = make_entity(db, label="t", status="dismissed")
    source = make_entity(db, label="s")
    assert _merge(client, source, target).status_code == 409


def _undo_merge(db, detail):
    """Reverse a merge using only its audit detail (test helper; proves the detail is sufficient)."""
    src, tgt = detail["source_entity_id"], detail["target_entity_id"]
    moved = detail["moved"]
    db.execute("UPDATE sighting SET entity_id=%s WHERE sighting_id = ANY(%s)", (src, moved["sighting_ids"]))
    db.execute("UPDATE event SET entity_id=%s WHERE event_id = ANY(%s::uuid[])", (src, moved["event_ids"]))
    db.execute("UPDATE entity_exemplar SET entity_id=%s WHERE exemplar_id = ANY(%s)", (src, moved["exemplar_ids"]))
    db.execute("UPDATE event SET actor_entity=%s WHERE event_id = ANY(%s::uuid[])", (src, moved["actor_event_ids"]))
    db.execute("UPDATE entity SET parent_entity=%s WHERE entity_id = ANY(%s::uuid[])", (src, moved["child_entity_ids"]))
    db.execute("UPDATE review_item SET entity_id=%s WHERE review_id = ANY(%s::uuid[])", (src, moved["review_ids"]))
    for rid, cands in moved["candidate_reviews_previous"].items():
        db.execute("UPDATE review_item SET candidate_ids=%s::uuid[] WHERE review_id=%s", (cands, rid))
    for d in detail["dropped_exemplars"]:
        db.execute(
            "INSERT INTO entity_exemplar (exemplar_id, entity_id, embedding, quality, crop_uri, created_at) "
            "VALUES (%s,%s,%s::halfvec,%s,%s,%s)",
            (d["exemplar_id"], d["entity_id"], d["embedding"], d["quality"], d["crop_uri"], d["created_at"]),
        )
    sp, tp = detail["source_previous"], detail["target_previous"]
    db.execute(
        "UPDATE entity SET status=%s, merged_into=NULL, needs_review=%s, review_reason=%s WHERE entity_id=%s",
        (sp["status"], sp["needs_review"], sp["review_reason"], src),
    )
    db.execute(
        "UPDATE entity SET last_seen_at=%s, last_location=%s, first_seen_at=%s, needs_review=%s, "
        "review_reason=%s WHERE entity_id=%s",
        (tp["last_seen_at"], tp["last_location"], tp["first_seen_at"], tp["needs_review"], tp["review_reason"], tgt),
    )


def _snapshot(db):
    return {
        "entity": db.execute(
            "SELECT entity_id, status, merged_into, last_seen_at, last_location, first_seen_at, "
            "needs_review, review_reason, parent_entity FROM entity ORDER BY entity_id"
        ).fetchall(),
        "sighting": db.execute("SELECT sighting_id, entity_id FROM sighting ORDER BY 1").fetchall(),
        "event": db.execute("SELECT event_id, entity_id, actor_entity FROM event ORDER BY 1").fetchall(),
        "exemplar": db.execute(
            "SELECT exemplar_id, entity_id, quality, crop_uri FROM entity_exemplar ORDER BY 1"
        ).fetchall(),
        "review": db.execute("SELECT review_id, entity_id, candidate_ids FROM review_item ORDER BY 1").fetchall(),
    }


def test_ac_api_09_merge_detail_is_sufficient_to_undo(client, db):
    target = make_entity(
        db, label="drill", status="confirmed", last_location="LAB1S2", last_seen_at=ts(10), first_seen_at=ts(5)
    )
    source = make_entity(
        db,
        label="drill dup",
        last_location="NOD2D4",
        last_seen_at=ts(20),
        first_seen_at=ts(1),
        needs_review=True,
        review_reason="possible_duplicate",
    )
    make_sighting(db, source, seen_at=ts(20), location_id="NOD2D4")
    make_sighting(db, target, seen_at=ts(10), location_id="LAB1S2")
    make_event(db, source, occurred_at=ts(19), from_location="LAB1S2", to_location="NOD2D4")
    for i in range(12):
        make_exemplar(db, target, vec((i, 1.0)), quality=0.4 + i / 100, crop_uri=f"s3://{BUCKET}/t{i}.jpg")
        make_exemplar(db, source, vec((50 + i, 1.0)), quality=0.3 + i / 50)
    child = make_entity(db, label="inside")
    db.execute("UPDATE entity SET parent_entity=%s WHERE entity_id=%s", (source, child))
    other = make_entity(db, label="o")
    make_review(db, entity_id=source)
    make_review(db, entity_id=other, kind="possible_duplicate", candidate_ids=[source])
    before = _snapshot(db)

    assert _merge(client, source, target).status_code == 200
    detail = audit_rows(db, "merge")[0][3]
    tp = detail["target_previous"]
    assert tp["last_seen_at"] == ts(10).isoformat()
    assert tp["last_location"] == "LAB1S2"
    assert tp["first_seen_at"] == ts(5).isoformat()
    assert tp["needs_review"] is False
    assert detail["source_previous"]["status"] == "provisional"
    assert _snapshot(db) != before

    _undo_merge(db, detail)
    assert _snapshot(db) == before


# ====================================================================== F8: idempotent retry of /ingest/window
def test_ac_api_18_identical_retry_is_not_duplicated(client, db):
    body = payload()
    first = ok(_ingest(client, body), 201)
    after_first = table_counts(db)
    second = ok(_ingest(client, copy.deepcopy(body)), 201)
    assert second == first
    assert table_counts(db) == after_first


def test_ac_api_18_different_payload_same_window_is_recorded(client, db):
    body = payload()
    ok(_ingest(client, body), 201)
    body2 = copy.deepcopy(body)
    body2["assignments"][0]["sighting"]["confidence"] = 0.83
    ok(_ingest(client, body2), 201)
    assert db.execute("SELECT count(*) FROM observation_window").fetchone()[0] == 2


# ====================================================================== F9: operator upload into no_capture zone
@pytest.mark.parametrize("loc", [CQ, "NOD3F4"])
def test_ac_api_07_operator_photo_in_no_capture_zone_is_422(client, db, s3, loc):
    before = table_counts(db)
    r = client.post(
        "/items",
        data={"label": "towel", "location_id": loc, "operator": "amy"},
        files={"image": ("x.png", PNG_BYTES, "image/png")},
    )
    assert r.status_code == 422, r.text
    assert table_counts(db) == before
    assert s3.list_objects_v2(Bucket=BUCKET).get("KeyCount", 0) == 0


@pytest.mark.parametrize("label", [" ", "   \t "])
def test_ac_api_07_whitespace_only_label_is_422(client, db, label):
    r = client.post(
        "/items", data={"label": label, "location_id": "LAB1S2"}, files={"image": ("x.png", PNG_BYTES, "image/png")}
    )
    assert r.status_code == 422, r.text


# ====================================================================== F10: resolve actor mapping
def test_ac_api_21_resolve_with_operator_pipeline_is_not_recorded_as_pipeline(client, db):
    e = make_entity(db, label="x", needs_review=True)
    rv = make_review(db, entity_id=e)
    r = client.post(f"/review/{rv}/resolve", json={"action": "dismiss", "operator": "pipeline"})
    body = ok(r)
    assert body["resolved_by"] == "unattributed-operator"
    assert all(a[2] != "pipeline" for a in audit_rows(db) if a[0] == "dismiss")


# ====================================================================== F11: URI validation / no raw passthrough
@pytest.mark.parametrize(
    "field,where",
    [
        ("image_uri", "review"),
        ("frame_uri", "sighting"),
        ("crop_uri", "exemplar"),
        ("evidence_uri", "event"),
    ],
)
@pytest.mark.parametrize(
    "bad", ["javascript:alert(1)", "s3://other-bucket/x.jpg", f"s3://{BUCKET}/../x.jpg", "https://evil.example/x.jpg"]
)
def test_ac_api_18_ingest_rejects_uris_outside_our_bucket(client, db, field, where, bad):
    body = payload()
    target = {
        "review": body["reviews"][0],
        "sighting": body["assignments"][0]["sighting"],
        "exemplar": body["assignments"][0]["exemplar"],
        "event": body["events"][0],
    }[where]
    target[field] = bad
    before = table_counts(db)
    assert _ingest(client, body).status_code == 422
    assert table_counts(db) == before


def test_ac_api_12_review_image_url_never_passes_raw_strings(client, db):
    make_review(db, image_uri="javascript:alert(1)")
    rows = ok(client.get("/review"))
    assert rows[0]["image_url"] is None


# ====================================================================== F12: media streaming + S3 errors
class _Body:
    def __init__(self, data):
        self.data = data
        self.read_called = False
        self.closed = False

    def read(self, *a):
        self.read_called = True
        return self.data

    def iter_chunks(self, chunk_size=1024):
        for i in range(0, len(self.data), chunk_size):
            yield self.data[i : i + chunk_size]

    def close(self):
        self.closed = True


class _StubS3:
    def __init__(self, exc=None, body=None):
        self.exc, self.body = exc, body

    def get_object(self, Bucket, Key):
        if self.exc:
            raise self.exc
        return {"Body": self.body, "ContentType": "image/jpeg", "ContentLength": len(self.body.data)}


def _client_with_store(settings, store):
    return TestClient(create_app(settings=settings, object_store=store), raise_server_exceptions=False)


def test_ac_api_20_media_streams_without_reading_whole_object(db, settings):
    body = _Body(JPEG_BYTES * 2000)
    with _client_with_store(settings, S3ObjectStore(_StubS3(body=body), BUCKET)) as c:
        r = c.get("/media/frames/a.jpg")
    assert r.status_code == 200
    assert r.content == JPEG_BYTES * 2000
    assert r.headers["content-type"].startswith("image/jpeg")
    assert r.headers["x-content-type-options"] == "nosniff"
    assert not body.read_called
    assert body.closed


def test_ac_api_20_media_access_denied_is_502(db, settings):
    exc = ClientError({"Error": {"Code": "AccessDenied", "Message": "no"}}, "GetObject")
    with _client_with_store(settings, S3ObjectStore(_StubS3(exc=exc), BUCKET)) as c:
        assert c.get("/media/frames/a.jpg").status_code == 502


def test_ac_api_20_media_store_unreachable_is_503(db, settings):
    exc = EndpointConnectionError(endpoint_url="http://rustfs:9000")
    with _client_with_store(settings, S3ObjectStore(_StubS3(exc=exc), BUCKET)) as c:
        assert c.get("/media/frames/a.jpg").status_code == 503


# ====================================================================== F14: fuzzy q
def test_ac_api_05_q_matches_typos_by_trigram_similarity(client, db):
    e = make_entity(db, label="hammer", last_seen_at=ts(1))
    make_entity(db, label="laptop", last_seen_at=ts(2))
    body = ok(client.get("/items", params={"q": "hamer"}))
    assert [i["entity_id"] for i in body["items"]] == [e]
    assert body["total"] == 1
