"""AC-API-18 atomic /ingest/window, AC-API-19 privacy (no_capture), AC-API-21 pipeline actor, AC-API-23 stock.

Response contract decided here: 201 with {"window_id": <uuid>, "entities": {<ref>: <entity_id>, ...}}.
"""
from __future__ import annotations

import copy

import pytest

from tests.conftest import BUCKET, audit_rows, iso, make_entity, make_exemplar, table_counts, ts, vec

FRAME = f"s3://{BUCKET}/frames/cam-lab1-fwd/0001.jpg"
CROP = f"s3://{BUCKET}/crops/0001-a.jpg"


def payload(**over):
    p = {
        "window": {"camera_id": "cam-lab1-fwd", "t_start": iso(10), "t_end": iso(10.1),
                   "scene_summary": "Crew member in blue shirt moves a drill to the starboard rack",
                   "transcript": "I'll put the drill on rack two", "audio_events": [{"label": "speech"}],
                   "model": "Qwen2.5-Omni-7B@sha256:abc", "raw_uri": f"s3://{BUCKET}/raw/w1.json"},
        "assignments": [{
            "entity_id": None, "ref": "o1",
            "new_entity": {"catalog_id": "drill-pistol", "kind": "object", "status": "provisional",
                           "label": "pistol-grip drill", "description": "black drill", "attributes": {"color": "black"}},
            "sighting": {"location_id": "LAB1S2", "seen_at": iso(10), "bbox": [0.1, 0.2, 0.3, 0.4],
                         "confidence": 0.82, "frame_uri": FRAME, "description": "drill in hand"},
            "exemplar": {"embedding": vec((0, 1.0)), "quality": 0.7, "crop_uri": CROP},
        }],
        "events": [{"event_type": "moved", "entity_ref": "o1", "actor_desc": "crew member, blue shirt",
                    "from_location": "LAB1P3", "to_location": "LAB1S2", "occurred_at": iso(10), "confidence": 0.7,
                    "evidence_uri": FRAME, "source": "pipeline"}],
        "reviews": [{"kind": "low_confidence", "entity_ref": "o1", "candidate_ids": [], "image_uri": CROP,
                     "description": "drill or driver?", "observed_at": iso(10), "location_id": "LAB1S2",
                     "model_guess": "drill-pistol", "probabilities": {"drill-pistol": 0.6, "NEW": 0.4}}],
        "decisions": [{"question": "same_or_new", "subject": {"ref": "o1"}, "choice": "NEW",
                       "probabilities": {"NEW": 0.8}, "model": "diffusiongemma", "latency_ms": 120}],
    }
    p.update(over)
    return p


def _ingest(client, body):
    return client.post("/ingest/window", json=body)


# ------------------------------------------------------------------ AC-API-18 happy path
def test_ac_api_18_full_window_is_recorded(client, db):
    r = _ingest(client, payload())
    assert r.status_code == 201, r.text
    body = r.json()
    wid, eid = body["window_id"], body["entities"]["o1"]
    w = db.execute("SELECT camera_id, scene_summary, transcript, model, raw_uri FROM observation_window "
                   "WHERE window_id=%s", (wid,)).fetchone()
    assert w == ("cam-lab1-fwd", payload()["window"]["scene_summary"], payload()["window"]["transcript"],
                 "Qwen2.5-Omni-7B@sha256:abc", f"s3://{BUCKET}/raw/w1.json")
    e = db.execute("SELECT catalog_id, kind, status, label, last_location, last_seen_at, confidence, snapshot_uri, "
                   "created_by, first_seen_at FROM entity WHERE entity_id=%s", (eid,)).fetchone()
    assert e[:5] == ("drill-pistol", "object", "provisional", "pistol-grip drill", "LAB1S2")
    assert e[5] == ts(10)
    assert e[6] == pytest.approx(0.82)
    assert e[7] in (FRAME, CROP)
    assert e[8] == "pipeline"
    assert e[9] == ts(10)
    s = db.execute("SELECT window_id, location_id, seen_at, confidence, frame_uri, bbox FROM sighting "
                   "WHERE entity_id=%s", (eid,)).fetchall()
    assert len(s) == 1
    assert str(s[0][0]) == wid and s[0][1] == "LAB1S2" and s[0][4] == FRAME
    assert s[0][5] == pytest.approx([0.1, 0.2, 0.3, 0.4])
    ex = db.execute("SELECT quality, crop_uri FROM entity_exemplar WHERE entity_id=%s", (eid,)).fetchall()
    assert len(ex) == 1 and ex[0][1] == CROP
    ev = db.execute("SELECT event_type, actor_desc, from_location, to_location, source FROM event "
                    "WHERE entity_id=%s", (eid,)).fetchall()
    assert ev == [("moved", "crew member, blue shirt", "LAB1P3", "LAB1S2", "pipeline")]
    rv = db.execute("SELECT kind, image_uri, status, model_guess FROM review_item WHERE entity_id=%s",
                    (eid,)).fetchall()
    assert rv == [("low_confidence", CROP, "open", "drill-pistol")]
    dl = db.execute("SELECT question, choice, model, latency_ms FROM decision_log").fetchall()
    assert dl == [("same_or_new", "NEW", "diffusiongemma", 120)]


def test_ac_api_18_minimal_payload_window_only(client, db):
    r = _ingest(client, {"window": payload()["window"], "assignments": []})
    assert r.status_code == 201
    assert db.execute("SELECT count(*) FROM observation_window").fetchone()[0] == 1


def test_ac_api_18_existing_entity_updated_when_sighting_newer(client, db):
    e = make_entity(db, label="drill", status="confirmed", last_location="LAB1P3", last_seen_at=ts(5),
                    confidence=0.5, snapshot_uri=f"s3://{BUCKET}/old.jpg")
    body = payload(events=[], reviews=[], decisions=[])
    body["assignments"][0] = {"entity_id": e, "sighting": {"location_id": "LAB1S2", "seen_at": iso(10),
                                                           "confidence": 0.9, "frame_uri": FRAME}}
    assert _ingest(client, body).status_code == 201
    row = db.execute("SELECT last_location, last_seen_at, confidence, snapshot_uri, status, label FROM entity "
                     "WHERE entity_id=%s", (e,)).fetchone()
    assert row[0] == "LAB1S2" and row[1] == ts(10)
    assert row[2] == pytest.approx(0.9)
    assert row[3] == FRAME
    assert row[4:] == ("confirmed", "drill")  # identity fields untouched


def test_ac_api_18_existing_entity_not_rolled_back_by_older_sighting(client, db):
    e = make_entity(db, label="drill", last_location="NOD2D4", last_seen_at=ts(50), confidence=0.6,
                    snapshot_uri=f"s3://{BUCKET}/new.jpg")
    body = payload(events=[], reviews=[], decisions=[])
    body["assignments"][0] = {"entity_id": e, "sighting": {"location_id": "LAB1S2", "seen_at": iso(10),
                                                           "confidence": 0.99, "frame_uri": FRAME}}
    assert _ingest(client, body).status_code == 201
    row = db.execute("SELECT last_location, last_seen_at, confidence, snapshot_uri FROM entity WHERE entity_id=%s",
                     (e,)).fetchone()
    assert row[0] == "NOD2D4" and row[1] == ts(50) and row[2] == pytest.approx(0.6)
    assert row[3] == f"s3://{BUCKET}/new.jpg"
    assert db.execute("SELECT count(*) FROM sighting WHERE entity_id=%s", (e,)).fetchone()[0] == 1  # still stored


def test_ac_api_18_two_sightings_same_payload_latest_wins(client, db):
    e = make_entity(db, label="cam", last_location="LAB1P3", last_seen_at=ts(0))
    body = payload(events=[], reviews=[], decisions=[])
    body["assignments"] = [
        {"entity_id": e, "sighting": {"location_id": "NOD2D4", "seen_at": iso(10.05), "confidence": 0.8}},
        {"entity_id": e, "sighting": {"location_id": "LAB1S2", "seen_at": iso(10.01), "confidence": 0.8}},
    ]
    assert _ingest(client, body).status_code == 201
    row = db.execute("SELECT last_location, last_seen_at FROM entity WHERE entity_id=%s", (e,)).fetchone()
    assert row == ("NOD2D4", ts(10.05))


def test_ac_api_18_refs_link_events_and_reviews_to_multiple_new_entities(client, db):
    body = payload(decisions=[])
    second = copy.deepcopy(body["assignments"][0])
    second["ref"] = "p1"
    second["new_entity"] = {"kind": "person", "status": "provisional", "label": "crew member, blue shirt",
                            "catalog_id": "crew-member"}
    second["exemplar"] = None
    body["assignments"].append(second)
    body["events"][0]["entity_ref"] = "p1"
    body["events"][0]["event_type"] = "used"
    r = _ingest(client, body)
    assert r.status_code == 201, r.text
    ents = r.json()["entities"]
    assert set(ents) == {"o1", "p1"} and ents["o1"] != ents["p1"]
    assert db.execute("SELECT kind FROM entity WHERE entity_id=%s", (ents["p1"],)).fetchone()[0] == "person"
    assert db.execute("SELECT count(*) FROM event WHERE entity_id=%s", (ents["p1"],)).fetchone()[0] == 1
    assert db.execute("SELECT count(*) FROM review_item WHERE entity_id=%s", (ents["o1"],)).fetchone()[0] == 1


def test_ac_api_18_events_and_reviews_may_use_existing_entity_id(client, db):
    e = make_entity(db, label="laptop", catalog_id="laptop")
    body = payload(decisions=[])
    body["events"][0].pop("entity_ref")
    body["events"][0]["entity_id"] = e
    body["reviews"][0].pop("entity_ref")
    body["reviews"][0]["entity_id"] = e
    body["reviews"][0]["candidate_ids"] = [e]
    assert _ingest(client, body).status_code == 201
    assert db.execute("SELECT count(*) FROM event WHERE entity_id=%s", (e,)).fetchone()[0] == 1
    assert db.execute("SELECT count(*) FROM review_item WHERE entity_id=%s", (e,)).fetchone()[0] == 1


def test_ac_api_18_exemplars_capped_at_20_dropping_lowest_quality(client, db):
    e = make_entity(db, label="laptop")
    for i in range(20):
        make_exemplar(db, e, vec((i, 1.0)), quality=0.30 + i * 0.01)  # 0.30 .. 0.49
    body = payload(events=[], reviews=[], decisions=[])
    body["assignments"][0] = {"entity_id": e, "sighting": {"location_id": "LAB1S2", "seen_at": iso(10),
                                                           "confidence": 0.9},
                              "exemplar": {"embedding": vec((100, 1.0)), "quality": 0.95, "crop_uri": CROP}}
    assert _ingest(client, body).status_code == 201
    q = sorted(r[0] for r in db.execute("SELECT quality FROM entity_exemplar WHERE entity_id=%s", (e,)))
    assert len(q) == 20
    assert min(q) == pytest.approx(0.31, abs=1e-4)  # 0.30 dropped
    assert max(q) == pytest.approx(0.95, abs=1e-4)


def test_ac_api_18_low_quality_exemplar_not_kept_when_gallery_full(client, db):
    e = make_entity(db, label="laptop")
    for i in range(20):
        make_exemplar(db, e, vec((i, 1.0)), quality=0.5 + i * 0.01)
    body = payload(events=[], reviews=[], decisions=[])
    body["assignments"][0] = {"entity_id": e, "sighting": {"location_id": "LAB1S2", "seen_at": iso(10),
                                                           "confidence": 0.9},
                              "exemplar": {"embedding": vec((100, 1.0)), "quality": 0.1}}
    assert _ingest(client, body).status_code == 201
    q = [r[0] for r in db.execute("SELECT quality FROM entity_exemplar WHERE entity_id=%s", (e,))]
    assert len(q) == 20
    assert min(q) == pytest.approx(0.5, abs=1e-4)


# ------------------------------------------------------------------ AC-API-18 atomicity
def _mutate(path_fn):
    body = payload()
    path_fn(body)
    return body


INVALID = {
    "unknown_camera": lambda b: b["window"].__setitem__("camera_id", "cam-nope"),
    "t_end_before_t_start": lambda b: b["window"].__setitem__("t_end", iso(5)),
    "missing_model": lambda b: b["window"].pop("model"),
    "missing_window": lambda b: b.pop("window"),
    "unknown_existing_entity": lambda b: b["assignments"][0].update(
        entity_id="00000000-0000-4000-8000-000000000000", new_entity=None),
    "no_entity_id_and_no_new_entity": lambda b: b["assignments"][0].pop("new_entity"),
    "sighting_confidence_gt_1": lambda b: b["assignments"][0]["sighting"].__setitem__("confidence", 1.5),
    "sighting_missing_seen_at": lambda b: b["assignments"][0]["sighting"].pop("seen_at"),
    "unknown_location": lambda b: b["assignments"][0]["sighting"].__setitem__("location_id", "MARS1"),
    "unknown_catalog": lambda b: b["assignments"][0]["new_entity"].__setitem__("catalog_id", "no-such"),
    "bad_entity_status": lambda b: b["assignments"][0]["new_entity"].__setitem__("status", "merged"),
    "bad_entity_kind": lambda b: b["assignments"][0]["new_entity"].__setitem__("kind", "robot"),
    "exemplar_wrong_dim": lambda b: b["assignments"][0]["exemplar"].__setitem__("embedding", [0.1] * 10),
    "duplicate_ref": lambda b: b["assignments"].append(copy.deepcopy(b["assignments"][0])),
    "event_unknown_ref": lambda b: b["events"][0].__setitem__("entity_ref", "zz"),
    "event_bad_type": lambda b: b["events"][0].__setitem__("event_type", "teleported"),
    "event_bad_source": lambda b: b["events"][0].__setitem__("source", "telepathy"),
    "event_missing_occurred_at": lambda b: b["events"][0].pop("occurred_at"),
    "review_unknown_ref": lambda b: b["reviews"][0].__setitem__("entity_ref", "zz"),
    "review_bad_kind": lambda b: b["reviews"][0].__setitem__("kind", "vibes"),
    "review_missing_image": lambda b: b["reviews"][0].pop("image_uri"),
    "review_empty_image": lambda b: b["reviews"][0].__setitem__("image_uri", ""),
    "review_missing_description": lambda b: b["reviews"][0].pop("description"),
    "decision_missing_choice": lambda b: b["decisions"][0].pop("choice"),
    "stock_negative": lambda b: b.__setitem__("stock_updates", [
        {"catalog_id": "food-pack", "location_id": "NOD1D1", "est_count": -1, "seen_at": iso(10)}]),
    "stock_unknown_catalog": lambda b: b.__setitem__("stock_updates", [
        {"catalog_id": "no-such", "location_id": "NOD1D1", "est_count": 3, "seen_at": iso(10)}]),
}


@pytest.mark.parametrize("case", sorted(INVALID))
def test_ac_api_18_any_invalid_part_is_422_and_nothing_written(client, db, case):
    existing = make_entity(db, label="pre-existing", last_location="NOD1D1", last_seen_at=ts(0))
    before = table_counts(db)
    before_row = db.execute("SELECT last_location, last_seen_at FROM entity WHERE entity_id=%s",
                            (existing,)).fetchone()
    body = _mutate(INVALID[case])
    # also touch the pre-existing entity so a partial write would be visible
    if body.get("assignments") and case != "unknown_existing_entity":
        body["assignments"].append({"entity_id": existing, "sighting": {"location_id": "LAB1S2",
                                                                       "seen_at": iso(10), "confidence": 0.9}})
    r = _ingest(client, body)
    assert r.status_code == 422, f"{case}: {r.status_code} {r.text}"
    assert table_counts(db) == before, case
    assert db.execute("SELECT last_location, last_seen_at FROM entity WHERE entity_id=%s",
                      (existing,)).fetchone() == before_row


def test_ac_api_18_late_db_failure_rolls_back_everything(client, db):
    # Everything is well-formed, but the LAST event points at an entity id that does not exist (only detectable
    # against the DB, after the window/entity/sighting inserts). The whole window must be rolled back.
    body = payload()
    body["events"].append({"event_type": "moved", "entity_id": "00000000-0000-4000-8000-0000000000ff",
                           "occurred_at": iso(10), "confidence": 0.5})
    before = table_counts(db)
    r = _ingest(client, body)
    assert r.status_code == 422
    assert table_counts(db) == before


# ------------------------------------------------------------------ AC-API-19 privacy
def _no_capture_body():
    body = payload(events=[], reviews=[], decisions=[])
    body["assignments"][0]["sighting"]["location_id"] = "NOD2S5"  # crew quarters, no_capture
    return body


def test_ac_api_19_no_capture_sighting_stored_without_frame_uri(client, db):
    r = _ingest(client, _no_capture_body())
    assert r.status_code == 201, r.text
    eid = r.json()["entities"]["o1"]
    frame_uri, loc = db.execute("SELECT frame_uri, location_id FROM sighting WHERE entity_id=%s", (eid,)).fetchone()
    assert loc == "NOD2S5"
    assert frame_uri is None


def test_ac_api_19_no_capture_entity_snapshot_and_exemplar_crop_not_stored(client, db):
    r = _ingest(client, _no_capture_body())
    assert r.status_code == 201
    eid = r.json()["entities"]["o1"]
    assert db.execute("SELECT snapshot_uri FROM entity WHERE entity_id=%s", (eid,)).fetchone()[0] is None
    crops = [c[0] for c in db.execute("SELECT crop_uri FROM entity_exemplar WHERE entity_id=%s", (eid,))]
    assert all(c is None for c in crops)


def test_ac_api_19_no_capture_existing_entity_keeps_old_snapshot(client, db):
    e = make_entity(db, label="headset", last_location="NOD2D4", last_seen_at=ts(1),
                    snapshot_uri=f"s3://{BUCKET}/ok.jpg")
    body = payload(events=[], reviews=[], decisions=[])
    body["assignments"][0] = {"entity_id": e, "sighting": {"location_id": "NOD2D5", "seen_at": iso(10),
                                                           "confidence": 0.9, "frame_uri": FRAME}}
    assert _ingest(client, body).status_code == 201
    row = db.execute("SELECT snapshot_uri, last_location FROM entity WHERE entity_id=%s", (e,)).fetchone()
    assert row[0] != FRAME
    assert row[1] == "NOD2D5"


def test_ac_api_19_no_capture_event_has_no_evidence_uri(client, db):
    body = _no_capture_body()
    body["events"] = [{"event_type": "moved", "entity_ref": "o1", "from_location": "NOD2D4",
                       "to_location": "NOD2S5", "occurred_at": iso(10), "confidence": 0.8, "evidence_uri": FRAME}]
    r = _ingest(client, body)
    assert r.status_code == 201
    eid = r.json()["entities"]["o1"]
    assert db.execute("SELECT evidence_uri FROM event WHERE entity_id=%s", (eid,)).fetchone()[0] is None


def test_ac_api_19_no_capture_review_rejected_422_nothing_written(client, db):
    body = payload(decisions=[])
    body["reviews"][0]["location_id"] = "NOD3F4"  # WHC, no_capture
    before = table_counts(db)
    r = _ingest(client, body)
    assert r.status_code == 422
    assert table_counts(db) == before


def test_ac_api_19_review_of_entity_sighted_in_no_capture_zone_rejected(client, db):
    body = _no_capture_body()
    body["reviews"] = [{"kind": "unknown_object", "entity_ref": "o1", "image_uri": CROP,
                        "description": "unknown in crew quarters", "observed_at": iso(10), "location_id": "NOD2S5"}]
    before = table_counts(db)
    assert _ingest(client, body).status_code == 422
    assert table_counts(db) == before


def test_ac_api_19_capture_allowed_zone_keeps_uris(client, db):
    r = _ingest(client, payload())
    assert r.status_code == 201, r.text
    eid = r.json()["entities"]["o1"]
    assert db.execute("SELECT frame_uri FROM sighting WHERE entity_id=%s", (eid,)).fetchone()[0] == FRAME


# ------------------------------------------------------------------ AC-API-21 (pipeline actor)
def test_ac_api_21_ingest_audits_created_entities_with_pipeline_actor(client, db):
    r = _ingest(client, payload())
    assert r.status_code == 201
    eid = r.json()["entities"]["o1"]
    rows = [a for a in audit_rows(db) if eid in [str(x) for x in a[1]]]
    assert rows
    assert all(a[2] == "pipeline" for a in rows)
    assert any(a[0] == "create" for a in rows)


# ------------------------------------------------------------------ AC-API-23 stock
def _stock_body(*updates):
    return {"window": payload()["window"], "assignments": [], "stock_updates": list(updates)}


def _su(catalog_id="food-pack", location_id="NOD1D1", est_count=10, seen=10):
    return {"catalog_id": catalog_id, "location_id": location_id, "est_count": est_count, "seen_at": iso(seen)}


def _stock(db):
    return {(r[0], r[1]): (r[2], r[3]) for r in db.execute(
        "SELECT catalog_id, location_id, est_count, last_seen_at FROM stock")}


def test_ac_api_23_stock_insert(client, db):
    assert _ingest(client, _stock_body(_su(), _su("wipes", "LAB1S2", 3))).status_code == 201
    assert _stock(db) == {("food-pack", "NOD1D1"): (10, ts(10)), ("wipes", "LAB1S2"): (3, ts(10))}


def test_ac_api_23_stock_newer_wins(client, db):
    assert _ingest(client, _stock_body(_su(est_count=10, seen=10))).status_code == 201
    assert _ingest(client, _stock_body(_su(est_count=7, seen=20))).status_code == 201
    assert _stock(db) == {("food-pack", "NOD1D1"): (7, ts(20))}


def test_ac_api_23_stock_older_update_ignored(client, db):
    assert _ingest(client, _stock_body(_su(est_count=7, seen=20))).status_code == 201
    assert _ingest(client, _stock_body(_su(est_count=99, seen=5))).status_code == 201
    assert _stock(db) == {("food-pack", "NOD1D1"): (7, ts(20))}


def test_ac_api_23_stock_zero_count_allowed(client, db):
    assert _ingest(client, _stock_body(_su(est_count=0))).status_code == 201
    assert _stock(db)[("food-pack", "NOD1D1")][0] == 0


@pytest.mark.parametrize("bad", [
    {"location_id": "MARS1"}, {"est_count": -3}, {"catalog_id": "no-such"}, {"seen_at": "nope"},
])
def test_ac_api_23_invalid_stock_update_is_422_atomic(client, db, bad):
    su = dict(_su(), **bad)
    before = table_counts(db)
    assert _ingest(client, _stock_body(_su("wipes"), su)).status_code == 422
    assert table_counts(db) == before


def test_ac_api_23_get_stock_lists_and_filters(client, db):
    assert _ingest(client, _stock_body(_su("food-pack", "NOD1D1", 10), _su("food-pack", "NOD2D4", 4),
                                       _su("wipes", "NOD2O4", 2), _su("wipes", "LAB1S2", 6))).status_code == 201
    r = client.get("/stock")
    assert r.status_code == 200
    rows = r.json()
    rows = rows if isinstance(rows, list) else rows["items"]
    assert len(rows) == 4
    for row in rows:
        assert {"catalog_id", "location_id", "est_count", "last_seen_at"} <= set(row)

    def keys(params):
        resp = client.get("/stock", params=params)
        assert resp.status_code == 200
        data = resp.json()
        data = data if isinstance(data, list) else data["items"]
        return {(x["catalog_id"], x["location_id"]) for x in data}

    assert keys({"catalog_id": "wipes"}) == {("wipes", "NOD2O4"), ("wipes", "LAB1S2")}
    assert keys({"location": "NOD2"}) == {("food-pack", "NOD2D4"), ("wipes", "NOD2O4")}
    assert keys({"location": "USOS"}) == {("food-pack", "NOD1D1"), ("food-pack", "NOD2D4"), ("wipes", "NOD2O4"),
                                          ("wipes", "LAB1S2")}
    assert keys({"catalog_id": "food-pack", "location": "NOD2"}) == {("food-pack", "NOD2D4")}
    assert keys({"location": "NOD2D4"}) == {("food-pack", "NOD2D4")}
