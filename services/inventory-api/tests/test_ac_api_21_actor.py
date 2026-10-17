"""AC-API-21: every write endpoint records the actor (operator name or 'pipeline') in audit_log."""
from __future__ import annotations

from tests.conftest import PNG_BYTES, audit_rows, iso, make_entity, make_review


def _new_rows(db, n_before):
    return audit_rows(db)[n_before:]


def test_ac_api_21_post_items_records_operator(client, db):
    n = len(audit_rows(db))
    r = client.post("/items", data={"label": "x", "location_id": "LAB1", "operator": "dave"},
                    files={"image": ("x.png", PNG_BYTES, "image/png")})
    assert r.status_code == 201
    rows = _new_rows(db, n)
    assert rows and all(a[2] == "dave" for a in rows)


def test_ac_api_21_patch_records_operator(client, db):
    e = make_entity(db, label="x")
    n = len(audit_rows(db))
    assert client.patch(f"/items/{e}", json={"label": "y", "operator": "erin"}).status_code == 200
    rows = _new_rows(db, n)
    assert rows and all(a[2] == "erin" for a in rows)


def test_ac_api_21_merge_records_operator(client, db):
    a = make_entity(db, label="a")
    b = make_entity(db, label="b")
    n = len(audit_rows(db))
    assert client.post(f"/items/{a}/merge", json={"into_entity_id": b, "operator": "frank"}).status_code == 200
    rows = _new_rows(db, n)
    assert rows and all(r[2] == "frank" for r in rows)


def test_ac_api_21_resolve_records_operator(client, db):
    e = make_entity(db, label="u", status="unknown", needs_review=True)
    rid = make_review(db, entity_id=e)
    n = len(audit_rows(db))
    assert client.post(f"/review/{rid}/resolve", json={"action": "dismiss", "operator": "gina"}).status_code == 200
    rows = _new_rows(db, n)
    assert rows and all(r[2] == "gina" for r in rows)


def test_ac_api_21_post_catalog_records_actor(client, db):
    n = len(audit_rows(db))
    r = client.post("/catalog", json={"catalog_id": "spork", "class_name": "spork", "display_name": "Spork",
                                      "operator": "hank"})
    assert r.status_code == 201
    rows = _new_rows(db, n)
    assert len(rows) == 1
    assert rows[0][2] == "hank"
    assert "spork" in str(rows[0][3])


def test_ac_api_21_write_without_operator_still_has_nonempty_actor(client, db):
    e = make_entity(db, label="x")
    n = len(audit_rows(db))
    assert client.patch(f"/items/{e}", json={"label": "z"}).status_code == 200
    rows = _new_rows(db, n)
    assert rows and all(isinstance(r[2], str) and r[2].strip() and r[2] != "pipeline" for r in rows)


def test_ac_api_21_ingest_records_pipeline(client, db):
    n = len(audit_rows(db))
    body = {"window": {"camera_id": "cam-lab1-fwd", "t_start": iso(1), "t_end": iso(1.1), "model": "m"},
            "assignments": [{"ref": "a", "new_entity": {"label": "thing", "kind": "object", "status": "unknown"},
                             "sighting": {"location_id": "LAB1S2", "seen_at": iso(1), "confidence": 0.4}}]}
    assert client.post("/ingest/window", json=body).status_code == 201
    rows = _new_rows(db, n)
    assert rows and all(r[2] == "pipeline" for r in rows)
