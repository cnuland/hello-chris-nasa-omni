"""AC-API-24..32 (UI-review contract additions) read through real HTTP on the e2e pipeline data.

The dashboard codes against these exact field names. Each response is also validated against the OpenAPI component
schemas. The tests that write (an operator item, one extra duplicate window) run last in this file and touch only data
they create, so the scene assertions in test_e2e_pipeline.py are unaffected.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta

import pytest

from conftest import BUCKET
from test_e2e_contracts import errors, ref

# 1x1 PNG
PNG_BYTES_E2E = bytes.fromhex(
    "89504e470d0a1a0a0000000d4948445200000001000000010806000000"
    "1f15c4890000000d49444154789c6360000002000154a24f5d0000000049454e44ae426082"
)
RICH_KEYS = {
    "entity_id", "label", "catalog_id", "status", "last_location", "last_location_name", "last_seen_at",
    "confidence", "thumbnail_url", "sighting_count",
}


def _dt(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _drill(api):
    return api.get("/items", params={"catalog_id": "drill-pistol"}).json()["items"][0]


def test_e2e_ui_review_queue_subject_bbox_total_and_detail(replay, api):
    resp = api.get("/review")
    assert resp.status_code == 200
    reviews = resp.json()
    assert isinstance(reviews, list) and reviews
    assert resp.headers["x-total-count"] == str(len(reviews))
    assert errors({"type": "array", "items": ref("ReviewItem")}, reviews) == []
    review = reviews[0]
    subject = review["subject"]
    assert RICH_KEYS <= set(subject) and subject["entity_id"] == review["entity_id"]
    assert subject["sighting_count"] >= 1
    assert review["candidates_source"] in {"stored", "suggested", "none"}
    bbox = review["image_bbox"]
    assert bbox is None or (len(bbox) == 4 and all(0.0 <= v <= 1.0 for v in bbox))
    assert api.get(f"/review/{review['review_id']}").json() == review
    assert api.get(f"/review/{uuid.uuid4()}").status_code == 404
    parents = {loc["location_id"]: loc["parent_id"] for loc in api.get("/locations").json()}
    parent = parents[review["location_id"]] or review["location_id"]
    in_parent = api.get("/review", params={"location": parent}).json()  # hierarchy, as /items
    assert review["review_id"] in {r["review_id"] for r in in_parent}
    assert api.get("/review", params={"q": "zzz-no-such-text"}).headers["x-total-count"] == "0"


def test_e2e_ui_review_events_carry_labels_and_types_filter(replay, api):
    events = api.get("/events", params={"limit": 1000}).json()
    assert events and errors({"type": "array", "items": ref("Event")}, events) == []
    labels = {i["entity_id"]: i["label"] for i in api.get("/items", params={"limit": 500}).json()["items"]}
    for ev in events:
        assert {"entity_label", "entity_catalog_id", "from_location_name", "to_location_name"} <= set(ev)
        if ev["entity_id"] in labels:
            assert ev["entity_label"] == labels[ev["entity_id"]]
        if ev["to_location"]:
            assert ev["to_location_name"]
    moved = api.get("/events", params={"types": "moved,handed_over"}).json()
    assert moved and {e["event_type"] for e in moved} <= {"moved", "handed_over"}
    assert api.get("/events", params={"types": "moved,teleported"}).status_code == 422


def test_e2e_ui_review_items_last_handled_and_provenance(replay, api, drill_move):
    drill = _drill(api)
    assert errors(ref("Item"), drill) == []
    assert drill["last_handled"]["event_type"] == "moved"
    assert drill["last_handled"]["actor_desc"] == drill_move.actor
    assert drill["location_set_by"] == "pipeline" and drill["location_set_at"]
    detail = api.get(f"/items/{drill['entity_id']}").json()
    assert detail["last_handled"] == drill["last_handled"]
    assert errors(ref("ItemDetail"), detail) == []


def test_e2e_ui_review_history_collapse_keeps_raw_audit(replay, api):
    drill = _drill(api)
    collapsed = api.get(f"/items/{drill['entity_id']}/history").json()
    raw = api.get(f"/items/{drill['entity_id']}/history", params={"collapse": "false"}).json()
    assert errors({"type": "array", "items": ref("TimelineEntry")}, collapsed) == []
    raw_audits = [h for h in raw if h["kind"] == "audit"]
    folded = [h for h in collapsed if h["kind"] == "audit"]
    assert raw_audits and sum(h["count"] for h in folded) == len(raw_audits)
    assert len(folded) <= len(raw_audits)
    assert [h for h in collapsed if h["kind"] != "audit"] == [h for h in raw if h["kind"] != "audit"]


# ------------------------------------------------------------------ writes (data created here only)
def test_e2e_ui_review_operator_move_does_not_fake_a_sighting(replay, api):
    created = api.post(
        "/items",
        data={"label": "e2e spare multimeter", "location_id": "LAB1S2", "operator": "e2e-op"},
        files={"image": ("m.png", PNG_BYTES_E2E, "image/png")},
    )
    assert created.status_code == 201, created.text
    item = created.json()
    assert item["last_handled"] is None  # operator creation is not "handling"
    moved = api.patch(f"/items/{item['entity_id']}", json={"last_location": "NOD1D1", "operator": "e2e-op"})
    assert moved.status_code == 200, moved.text
    after = moved.json()
    # (POST /items formats times as +00:00, other endpoints as Z: compare instants, not strings)
    assert _dt(after["last_seen_at"]) == _dt(item["last_seen_at"])
    assert after["location_set_by"] == "e2e-op" and _dt(after["location_set_at"]) >= _dt(item["location_set_at"])
    assert after["last_handled"]["event_type"] == "moved"
    api.patch(f"/items/{item['entity_id']}", json={"status": "dismissed", "operator": "e2e-op"})  # tidy up


def test_e2e_ui_review_duplicate_review_gets_server_suggestions(replay, api, dataset):
    drill = _drill(api)
    seen = dataset.manifest.start_time + timedelta(hours=1)
    frame = f"s3://{BUCKET}/frames/e2e-dup/0001.jpg"
    body = {
        "window": {"camera_id": dataset.manifest.camera, "t_start": seen.isoformat(), "t_end": seen.isoformat(),
                   "model": "e2e"},
        "assignments": [{
            "ref": "dup",
            "new_entity": {"catalog_id": "drill-pistol", "kind": "object", "status": "provisional",
                           "label": "drill"},
            "sighting": {"location_id": drill["last_location"], "seen_at": seen.isoformat(),
                         "bbox": [0.25, 0.25, 0.5, 0.75], "confidence": 0.6, "frame_uri": frame},
        }],
        "reviews": [{"kind": "possible_duplicate", "entity_ref": "dup", "candidate_ids": [], "image_uri": frame,
                     "description": "3 live 'drill' entities would exceed the expected count of 2",
                     "observed_at": seen.isoformat(), "location_id": drill["last_location"]}],
    }
    assert api.post("/ingest/window", json=body).status_code == 201
    dups = api.get("/review", params={"kind": "possible_duplicate"}).json()
    assert len(dups) == 1
    review = dups[0]
    assert errors(ref("ReviewItem"), review) == []
    assert review["candidate_ids"] == [] and review["candidates_source"] == "suggested"
    assert drill["entity_id"] in [c["entity_id"] for c in review["candidates"]]
    assert all(RICH_KEYS | {"similarity"} <= set(c) for c in review["candidates"])
    assert len(review["candidates"]) <= 8
    assert review["image_bbox"] == pytest.approx([0.25, 0.25, 0.5, 0.75])
    assert review["subject"]["label"] == "drill"


# ------------------------------------------------------------------ UI review round 2 (AC-API-33..38)
def test_e2e_ui_review2_items_events_and_sightings_carry_the_new_fields(replay, api):
    drill = _drill(api)
    for key in ("merged_into", "merged_at", "merged_from", "snapshot_bbox", "snapshot_bbox_source"):
        assert key in drill
    assert drill["merged_into"] is None and drill["merged_from"] == []
    sources = set()
    for item in api.get("/items", params={"limit": 500}).json()["items"]:
        detail = api.get(f"/items/{item['entity_id']}").json()
        assert errors(ref("ItemDetail"), detail) == []
        sources |= {s["bbox_source"] for s in detail["sightings"] if s["bbox"] is not None}
    # perception now says where its boxes came from (only the hand-written e2e duplicate window has no source)
    assert sources & {"detector", "omni"} and sources <= {"detector", "omni", None}
    events = api.get("/events", params={"limit": 1000}).json()
    assert errors({"type": "array", "items": ref("Event")}, events) == []
    for ev in events:
        assert ev["location_role"] in {"at", "from_to", None} and ev["actor_kind"] in {"operator", "person", None}
        if ev["event_type"] == "moved":
            assert ev["location_role"] == "from_to"
    labels = [i["label"] for i in api.get("/items", params={"sort": "label", "limit": 500}).json()["items"]]
    assert [x.lower() for x in labels] == sorted(x.lower() for x in labels)


def test_e2e_ui_review2_suggestions_are_scored_and_merge_can_be_undone(replay, api):
    dup = api.get("/review", params={"kind": "possible_duplicate"}).json()[0]  # created by the test above
    assert errors(ref("ReviewItem"), dup) == []
    assert "image_bbox_source" in dup
    cands = dup["candidates"]
    assert cands and all(isinstance(c["score"], float) and isinstance(c["match_reasons"], list) for c in cands)
    assert [c["score"] for c in cands] == sorted((c["score"] for c in cands), reverse=True)
    source, target = dup["entity_id"], cands[0]["entity_id"]
    before_target = api.get(f"/items/{target}").json()
    resolved = api.post(f"/review/{dup['review_id']}/resolve",
                        json={"action": "merge_into", "target_entity_id": target, "operator": "e2e-op"})
    assert resolved.status_code == 200, resolved.text
    merged = api.get(f"/items/{source}").json()
    assert merged["merged_into"]["entity_id"] == target and merged["merged_at"]
    assert source in [m["entity_id"] for m in api.get(f"/items/{target}").json()["merged_from"]]
    undone = api.post(f"/items/{source}/unmerge", json={"operator": "e2e-op", "note": "e2e undo"})
    assert undone.status_code == 200, undone.text
    body = undone.json()
    assert errors(ref("UnmergeResult"), body) == []
    assert body["restored"]["reopened_review_id"] == dup["review_id"]
    assert body["source"]["status"] == "provisional" and body["source"]["merged_into"] is None
    after_target = api.get(f"/items/{target}").json()
    for key in ("last_seen_at", "last_location", "first_seen_at", "needs_review", "location_set_at"):
        assert after_target[key] == before_target[key], key
    assert api.get(f"/review/{dup['review_id']}").json()["status"] == "open"
    assert api.post(f"/items/{source}/unmerge", json={"operator": "e2e-op"}).status_code == 409
