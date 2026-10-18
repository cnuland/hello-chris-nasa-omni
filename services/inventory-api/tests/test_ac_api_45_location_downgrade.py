"""AC-API-45 location downgrade hold (UI review round 5, finding UI5-05; perception AC-PER-62).

Live: person 28513d12 went NOD1 06:30 -> CUP1 06:31 -> USOS 06:32; 44% of items ended at segment level because a
later segment-only sighting (the handheld camera could not name the module) overwrote a module location. A coarser
sighting inside the same segment soon after refines the time, not the place."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from inventory_api.config import Settings
from inventory_api.main import create_app
from tests.conftest import BUCKET, iso, make_entity, ts
from tests.test_ac_api_18_19_23_ingest import payload


def _seen(client, e, location, minutes):
    body = payload(events=[], reviews=[], decisions=[])
    body["window"]["t_start"] = body["window"]["t_end"] = iso(minutes)
    body["window"]["camera_id"] = "cam-handheld"
    body["assignments"] = [{"entity_id": e, "sighting": {"location_id": location, "seen_at": iso(minutes),
                                                          "confidence": 0.9}}]
    r = client.post("/ingest/window", json=body)
    assert r.status_code == 201, r.text


def _row(db, e):
    return db.execute("SELECT last_location, last_seen_at, location_set_at FROM entity WHERE entity_id=%s",
                      (e,)).fetchone()


def _sightings(db, e):
    return [r[0] for r in db.execute("SELECT location_id FROM sighting WHERE entity_id=%s ORDER BY seen_at", (e,))]


def test_live_case_segment_sighting_soon_after_keeps_the_module(client, db):
    e = make_entity(db, label="person", kind="person", last_location="CUP1", last_seen_at=ts(0))
    _seen(client, e, "USOS", 1)
    loc, seen, set_at = _row(db, e)
    assert loc == "CUP1" and seen == ts(1)  # the time advances, the place is kept
    assert set_at == ts(0)  # provenance kept (a legacy row's last_seen_at is frozen as its set time)
    assert _sightings(db, e) == ["USOS"]  # the sighting itself stays segment-level


def test_zone_is_kept_too(client, db):
    e = make_entity(db, label="drill", last_location="LAB1S2", last_seen_at=ts(0))
    _seen(client, e, "USOS", 5)
    assert _row(db, e)[0] == "LAB1S2"


def test_hold_is_measured_from_when_the_module_was_set(client, db):
    e = make_entity(db, label="drill", last_location="JPM1", last_seen_at=ts(0))
    _seen(client, e, "USOS", 5)
    _seen(client, e, "USOS", 9)  # 9 min after the module was set: still held
    assert _row(db, e)[0] == "JPM1"
    _seen(client, e, "USOS", 10.5)  # 630 s after: the segment wins
    loc, seen, set_at = _row(db, e)
    assert (loc, seen, set_at) == ("USOS", ts(10.5), ts(10.5))


def test_other_segment_moves_the_item(client, db):
    e = make_entity(db, label="drill", last_location="JPM1", last_seen_at=ts(0))
    _seen(client, e, "ROS", 1)
    assert _row(db, e)[0] == "ROS"


def test_module_sighting_always_moves_the_item(client, db):
    e = make_entity(db, label="drill", last_location="JPM1", last_seen_at=ts(0))
    _seen(client, e, "ZVEZDA", 1)
    assert _row(db, e)[0] == "ZVEZDA"


def test_segment_to_segment_is_unchanged(client, db):
    e = make_entity(db, label="drill", last_location="USOS", last_seen_at=ts(0))
    _seen(client, e, "USOS", 1)
    assert _row(db, e)[:2] == ("USOS", ts(1))


def test_operator_placed_module_is_held_as_well(client, db):
    e = make_entity(db, label="drill", last_location="LAB1", last_seen_at=ts(0))
    db.execute("UPDATE entity SET location_set_at=%s, location_set_by='amy' WHERE entity_id=%s", (ts(2), e))
    _seen(client, e, "USOS", 4)
    assert _row(db, e) == ("LAB1", ts(4), ts(2))


@pytest.fixture()
def client_no_hold(db, database_url, store):
    app = create_app(settings=Settings(database_url=database_url, s3_bucket=BUCKET, location_downgrade_hold_s=0.0),
                     object_store=store)
    with TestClient(app, raise_server_exceptions=False) as c:
        yield c


def test_hold_can_be_turned_off(client_no_hold, db):
    e = make_entity(db, label="drill", last_location="JPM1", last_seen_at=ts(0))
    _seen(client_no_hold, e, "USOS", 1)
    assert _row(db, e)[0] == "USOS"


def test_setting_from_env(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql://x/y")
    assert Settings.from_env().location_downgrade_hold_s == 600.0
    monkeypatch.setenv("LOCATION_DOWNGRADE_HOLD_S", "180")
    assert Settings.from_env().location_downgrade_hold_s == 180.0


# ---------------------------------------------------------------- review round
def test_review_naive_seen_at_is_utc_not_a_500(client, db):
    e = make_entity(db, label="drill", last_location="JPM1", last_seen_at=ts(0))
    body = payload(events=[], reviews=[], decisions=[])
    naive = ts(1).replace(tzinfo=None).isoformat()
    body["window"]["t_start"] = body["window"]["t_end"] = iso(1)
    body["assignments"] = [{"entity_id": e, "sighting": {"location_id": "USOS", "seen_at": naive, "confidence": 0.9}}]
    r = client.post("/ingest/window", json=body)
    assert r.status_code == 201, r.text
    assert _row(db, e)[0] == "JPM1"


def test_review_late_sighting_does_not_freeze_provenance(client, db):
    e = make_entity(db, label="drill", last_location="JPM1", last_seen_at=ts(2))
    _seen(client, e, "USOS", 1)  # older than last_seen_at: the UPDATE does not apply
    assert _row(db, e) == ("JPM1", ts(2), None)
