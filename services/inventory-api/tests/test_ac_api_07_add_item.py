"""AC-API-7: POST /items (multipart) requires an image; stores it, creates entity + added event + audit row."""
from __future__ import annotations

import pytest

from tests.conftest import BUCKET, JPEG_BYTES, PNG_BYTES, audit_rows, table_counts

FORM = {"label": "Spare headset", "location_id": "NOD2D4", "operator": "flight-controller-amy",
        "description": "Comm headset found in deck bay", "catalog_id": "headset", "kind": "object"}


def _post(client, data=None, files=None):
    return client.post("/items", data=data if data is not None else FORM, files=files)


def test_ac_api_07_add_item_success_201(client, db, s3):
    r = _post(client, files={"image": ("headset.png", PNG_BYTES, "image/png")})
    assert r.status_code == 201, r.text
    item = r.json()
    assert item["label"] == "Spare headset"
    assert item["status"] == "confirmed"
    assert item["last_location"] == "NOD2D4"
    assert item["catalog_id"] == "headset"
    assert item["kind"] == "object"
    assert item["description"] == "Comm headset found in deck bay"
    assert item["snapshot_url"], "snapshot_url must be returned"
    assert item["snapshot_url"].startswith("/media/")


def test_ac_api_07_entity_row_created_by_operator(client, db):
    r = _post(client, files={"image": ("h.png", PNG_BYTES, "image/png")})
    assert r.status_code == 201
    eid = r.json()["entity_id"]
    row = db.execute("SELECT created_by, status, last_location, snapshot_uri, label, catalog_id FROM entity "
                     "WHERE entity_id=%s", (eid,)).fetchone()
    assert row is not None
    created_by, status, loc, snap, label, cat = row
    assert created_by == "operator"
    assert status == "confirmed"
    assert loc == "NOD2D4"
    assert label == "Spare headset"
    assert cat == "headset"
    assert snap and snap.startswith(f"s3://{BUCKET}/")


def test_ac_api_07_image_stored_in_object_store(client, db, s3):
    r = _post(client, files={"image": ("h.png", PNG_BYTES, "image/png")})
    assert r.status_code == 201
    snap = db.execute("SELECT snapshot_uri FROM entity WHERE entity_id=%s", (r.json()["entity_id"],)).fetchone()[0]
    key = snap.removeprefix(f"s3://{BUCKET}/")
    obj = s3.get_object(Bucket=BUCKET, Key=key)
    assert obj["Body"].read() == PNG_BYTES
    assert obj["ContentType"] == "image/png"


def test_ac_api_07_snapshot_url_is_served_by_media(client, db):
    r = _post(client, files={"image": ("h.jpg", JPEG_BYTES, "image/jpeg")})
    assert r.status_code == 201
    m = client.get(r.json()["snapshot_url"])
    assert m.status_code == 200
    assert m.content == JPEG_BYTES
    assert m.headers["content-type"].startswith("image/jpeg")


def test_ac_api_07_added_event_with_operator_source(client, db):
    r = _post(client, files={"image": ("h.png", PNG_BYTES, "image/png")})
    assert r.status_code == 201
    rows = db.execute("SELECT event_type, source, to_location FROM event WHERE entity_id=%s",
                      (r.json()["entity_id"],)).fetchall()
    assert len(rows) == 1
    assert rows[0][0] == "added"
    assert rows[0][1] == "operator"
    assert rows[0][2] == "NOD2D4"


def test_ac_api_07_audit_log_row_with_actor(client, db):
    r = _post(client, files={"image": ("h.png", PNG_BYTES, "image/png")})
    assert r.status_code == 201
    eid = r.json()["entity_id"]
    rows = [a for a in audit_rows(db, "create") if eid in [str(x) for x in a[1]]]
    assert len(rows) == 1
    assert rows[0][2] == "flight-controller-amy"


def test_ac_api_07_kind_defaults_to_object(client, db):
    data = {k: v for k, v in FORM.items() if k not in ("kind", "catalog_id")}
    r = _post(client, data=data, files={"image": ("h.png", PNG_BYTES, "image/png")})
    assert r.status_code == 201
    assert r.json()["kind"] == "object"


def test_ac_api_07_missing_image_is_422_and_nothing_written(client, db, s3):
    before = table_counts(db)
    r = _post(client)
    assert r.status_code == 422
    assert table_counts(db) == before
    assert s3.list_objects_v2(Bucket=BUCKET).get("KeyCount", 0) == 0


def test_ac_api_07_empty_image_is_422(client, db):
    before = table_counts(db)
    r = _post(client, files={"image": ("h.png", b"", "image/png")})
    assert r.status_code == 422
    assert table_counts(db) == before


def test_ac_api_07_non_image_content_type_is_422(client, db, s3):
    before = table_counts(db)
    r = _post(client, files={"image": ("notes.txt", b"hello world", "text/plain")})
    assert r.status_code == 422
    assert table_counts(db) == before
    assert s3.list_objects_v2(Bucket=BUCKET).get("KeyCount", 0) == 0


def test_ac_api_07_non_image_bytes_with_image_content_type_is_422(client, db):
    before = table_counts(db)
    r = _post(client, files={"image": ("fake.png", b"#!/bin/sh\nrm -rf /\n", "image/png")})
    assert r.status_code == 422
    assert table_counts(db) == before


@pytest.mark.parametrize("missing", ["label", "location_id"])
def test_ac_api_07_missing_required_field_is_422(client, db, missing):
    data = {k: v for k, v in FORM.items() if k != missing}
    before = table_counts(db)
    r = _post(client, data=data, files={"image": ("h.png", PNG_BYTES, "image/png")})
    assert r.status_code == 422
    assert table_counts(db) == before


@pytest.mark.parametrize("field,value", [("location_id", "MARS1"), ("catalog_id", "no-such-item"),
                                         ("kind", "robot")])
def test_ac_api_07_invalid_reference_or_enum_is_422_not_500(client, db, s3, field, value):
    data = dict(FORM, **{field: value})
    before = table_counts(db)
    r = _post(client, data=data, files={"image": ("h.png", PNG_BYTES, "image/png")})
    assert r.status_code == 422
    assert table_counts(db) == before
