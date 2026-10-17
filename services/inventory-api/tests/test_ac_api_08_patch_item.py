"""AC-API-8: PATCH /items/{id} partial update, moved event, audit before/after, 404, 409."""
from __future__ import annotations

import json
import uuid

import pytest

from tests.conftest import audit_rows, make_entity, table_counts, ts


@pytest.fixture()
def drill(db):
    return make_entity(db, label="drill", description="old desc", catalog_id="drill-pistol", status="confirmed",
                       last_location="LAB1S2", last_seen_at=ts(1), confidence=0.8, attributes={"color": "black"})


def _row(db, eid):
    return db.execute("SELECT label, description, catalog_id, last_location, status, attributes, needs_review, "
                      "confidence, last_seen_at FROM entity WHERE entity_id=%s", (eid,)).fetchone()


def test_ac_api_08_updates_only_provided_fields(client, db, drill):
    before = _row(db, drill)
    r = client.patch(f"/items/{drill}", json={"label": "PGT drill", "operator": "amy"})
    assert r.status_code == 200, r.text
    assert r.json()["label"] == "PGT drill"
    after = _row(db, drill)
    assert after[0] == "PGT drill"
    assert after[1:] == before[1:]


def test_ac_api_08_can_clear_catalog_id_with_null(client, db, drill):
    r = client.patch(f"/items/{drill}", json={"catalog_id": None, "operator": "amy"})
    assert r.status_code == 200
    assert _row(db, drill)[2] is None
    assert r.json()["catalog_id"] is None


def test_ac_api_08_updates_attributes_and_needs_review(client, db, drill):
    r = client.patch(f"/items/{drill}", json={"attributes": {"color": "yellow"}, "needs_review": True,
                                              "operator": "amy"})
    assert r.status_code == 200
    row = _row(db, drill)
    assert row[5] == {"color": "yellow"}
    assert row[6] is True


def test_ac_api_08_location_change_creates_moved_event(client, db, drill):
    r = client.patch(f"/items/{drill}", json={"last_location": "NOD2D4", "operator": "amy"})
    assert r.status_code == 200
    assert r.json()["last_location"] == "NOD2D4"
    events = db.execute("SELECT event_type, source, from_location, to_location FROM event WHERE entity_id=%s",
                        (drill,)).fetchall()
    assert events == [("moved", "operator", "LAB1S2", "NOD2D4")]


def test_ac_api_08_same_location_does_not_create_moved_event(client, db, drill):
    r = client.patch(f"/items/{drill}", json={"last_location": "LAB1S2", "label": "x", "operator": "amy"})
    assert r.status_code == 200
    assert db.execute("SELECT count(*) FROM event WHERE entity_id=%s", (drill,)).fetchone()[0] == 0


def test_ac_api_08_non_location_change_creates_no_event(client, db, drill):
    r = client.patch(f"/items/{drill}", json={"description": "new", "operator": "amy"})
    assert r.status_code == 200
    assert db.execute("SELECT count(*) FROM event WHERE entity_id=%s", (drill,)).fetchone()[0] == 0


def test_ac_api_08_audit_log_has_before_and_after(client, db, drill):
    r = client.patch(f"/items/{drill}", json={"label": "PGT drill", "last_location": "NOD2D4", "operator": "amy",
                                              "note": "found during stowage audit"})
    assert r.status_code == 200
    rows = [a for a in audit_rows(db, "update") if drill in [str(x) for x in a[1]]]
    assert len(rows) == 1
    _op, _entity_ids, actor, detail, _at = rows[0]
    assert actor == "amy"
    assert "before" in detail and "after" in detail
    assert detail["before"]["label"] == "drill"
    assert detail["after"]["label"] == "PGT drill"
    assert detail["before"]["last_location"] == "LAB1S2"
    assert detail["after"]["last_location"] == "NOD2D4"
    assert "found during stowage audit" in json.dumps(detail)


def test_ac_api_08_unknown_id_is_404(client, db):
    r = client.patch(f"/items/{uuid.uuid4()}", json={"label": "x", "operator": "amy"})
    assert r.status_code == 404


def test_ac_api_08_malformed_id_is_422_or_404_not_500(client, db):
    r = client.patch("/items/not-a-uuid", json={"label": "x", "operator": "amy"})
    assert r.status_code in (404, 422)


def test_ac_api_08_merged_entity_is_409_and_unchanged(client, db, drill):
    dup = make_entity(db, label="dup", status="merged", merged_into=drill)
    before = table_counts(db)
    r = client.patch(f"/items/{dup}", json={"label": "x", "operator": "amy"})
    assert r.status_code == 409
    assert db.execute("SELECT label FROM entity WHERE entity_id=%s", (dup,)).fetchone()[0] == "dup"
    assert table_counts(db) == before


@pytest.mark.parametrize("body", [
    {"status": "merged"},          # merging only via /merge
    {"status": "bogus"},
    {"last_location": "MARS1"},
    {"catalog_id": "no-such-item"},
    {"attributes": "not-an-object"},
])
def test_ac_api_08_invalid_values_are_422_and_nothing_written(client, db, drill, body):
    before_row = _row(db, drill)
    before = table_counts(db)
    r = client.patch(f"/items/{drill}", json=dict(body, operator="amy"))
    assert r.status_code == 422, r.text
    assert _row(db, drill) == before_row
    assert table_counts(db) == before
