"""AC-API-10 item detail, AC-API-11 history timeline."""
from __future__ import annotations

import uuid

from tests.conftest import BUCKET, make_entity, make_event, make_exemplar, make_sighting, ok, parse_dt, ts, vec


def test_ac_api_10_detail_basic_fields(client, db):
    e = make_entity(db, label="drill", catalog_id="drill-pistol", last_location="LAB1S2", last_seen_at=ts(5),
                    status="confirmed", confidence=0.9, snapshot_uri=f"s3://{BUCKET}/snap/drill.jpg")
    r = client.get(f"/items/{e}")
    assert r.status_code == 200
    d = r.json()
    assert {"entity_id", "kind", "status", "label", "confidence", "sightings", "events", "exemplar_count",
            "last_moved_by", "last_moved_at"} <= set(d)
    assert d["entity_id"] == e
    assert d["catalog_name"] == "Pistol-grip drill"
    assert d["snapshot_url"] == "/media/snap/drill.jpg"
    assert d["sightings"] == [] and d["events"] == [] and d["exemplar_count"] == 0
    assert d["last_moved_by"] is None and d["last_moved_at"] is None


def test_ac_api_10_detail_caps_sightings_and_events_at_20_newest_first(client, db):
    e = make_entity(db, label="laptop", last_seen_at=ts(30))
    for i in range(25):
        make_sighting(db, e, seen_at=ts(i), location_id="LAB1S2")
        make_event(db, e, event_type="seen", occurred_at=ts(i))
    d = ok(client.get(f"/items/{e}"))
    assert len(d["sightings"]) == 20
    assert len(d["events"]) == 20
    s_times = [parse_dt(s["seen_at"]) for s in d["sightings"]]
    e_times = [parse_dt(ev["occurred_at"]) for ev in d["events"]]
    assert s_times == sorted(s_times, reverse=True) and s_times[0] == ts(24) and s_times[-1] == ts(5)
    assert e_times == sorted(e_times, reverse=True) and e_times[0] == ts(24)


def test_ac_api_10_exemplar_count(client, db):
    e = make_entity(db, label="x")
    for i in range(3):
        make_exemplar(db, e, vec((i, 1.0)))
    assert ok(client.get(f"/items/{e}"))["exemplar_count"] == 3


def test_ac_api_10_last_moved_by_and_at_from_latest_moved_event(client, db):
    e = make_entity(db, label="camera")
    make_event(db, e, event_type="moved", occurred_at=ts(1), actor_desc="crew member, red shirt")
    make_event(db, e, event_type="moved", occurred_at=ts(9), actor_desc="crew member, blue shirt")
    make_event(db, e, event_type="used", occurred_at=ts(20), actor_desc="crew member, green shirt")
    d = ok(client.get(f"/items/{e}"))
    assert d["last_moved_by"] == "crew member, blue shirt"
    assert parse_dt(d["last_moved_at"]) == ts(9)


def test_ac_api_10_events_have_contract_fields(client, db):
    e = make_entity(db, label="camera")
    make_event(db, e, event_type="moved", occurred_at=ts(1), actor_desc="crew", from_location="LAB1S2",
               to_location="NOD2D4", evidence_uri=f"s3://{BUCKET}/ev/1.jpg")
    ev = ok(client.get(f"/items/{e}"))["events"][0]
    assert {"event_id", "event_type", "entity_id", "actor_desc", "from_location", "to_location", "occurred_at",
            "confidence", "evidence_url", "source"} <= set(ev)
    assert ev["evidence_url"] == "/media/ev/1.jpg"


def test_ac_api_10_unknown_item_is_404(client, db):
    assert client.get(f"/items/{uuid.uuid4()}").status_code == 404


def test_ac_api_10_malformed_id_not_500(client, db):
    assert client.get("/items/xyz").status_code in (404, 422)


# ------------------------------------------------------------------ AC-API-11
def test_ac_api_11_history_merges_sightings_events_audit_desc(client, db):
    e = make_entity(db, label="drill", last_location="LAB1S2")
    make_sighting(db, e, seen_at=ts(1), location_id="LAB1S2", frame_uri=f"s3://{BUCKET}/f/1.jpg")
    make_event(db, e, event_type="moved", occurred_at=ts(3), from_location="LAB1S2", to_location="NOD2D4")
    make_sighting(db, e, seen_at=ts(5), location_id="NOD2D4")
    db.execute("INSERT INTO audit_log (op, entity_ids, actor, detail, at) VALUES ('update', ARRAY[%s]::uuid[], "
               "'amy', '{}'::jsonb, %s)", (e, ts(4)))
    db.execute("INSERT INTO audit_log (op, entity_ids, actor, detail, at) VALUES ('update', ARRAY[%s]::uuid[], "
               "'amy', '{}'::jsonb, %s)", (str(uuid.uuid4()), ts(2)))  # other entity -> excluded
    r = client.get(f"/items/{e}/history")
    assert r.status_code == 200
    timeline = r.json()
    assert [t["kind"] for t in timeline] == ["sighting", "audit", "event", "sighting"]
    times = [parse_dt(t["at"]) for t in timeline]
    assert times == [ts(5), ts(4), ts(3), ts(1)]
    assert timeline[-1]["image_url"] == "/media/f/1.jpg"
    assert timeline[0]["location_id"] == "NOD2D4"
    assert all(isinstance(t.get("summary"), str) for t in timeline)


def test_ac_api_11_history_empty_for_new_item(client, db):
    e = make_entity(db, label="fresh")
    r = client.get(f"/items/{e}/history")
    assert r.status_code == 200
    assert r.json() == []


def test_ac_api_11_history_unknown_item_404(client, db):
    assert client.get(f"/items/{uuid.uuid4()}/history").status_code == 404
