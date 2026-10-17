"""AC-API-12 review queue and AC-API-13 resolve actions."""
from __future__ import annotations

import uuid

import pytest

from tests.conftest import BUCKET, audit_rows, make_entity, make_review, ok, parse_dt, table_counts, ts


# ------------------------------------------------------------------ AC-API-12
def test_ac_api_12_lists_open_items_newest_first(client, db):
    e1 = make_entity(db, label="unknown 1", status="unknown", needs_review=True)
    e2 = make_entity(db, label="unknown 2", status="unknown", needs_review=True)
    old = make_review(db, entity_id=e1, observed_at=ts(1), created_at=ts(1))
    new = make_review(db, entity_id=e2, observed_at=ts(5), created_at=ts(5))
    make_review(db, entity_id=e2, observed_at=ts(9), created_at=ts(9), status="resolved")
    r = client.get("/review")
    assert r.status_code == 200
    got = r.json()
    assert [x["review_id"] for x in got] == [new, old]
    for x in got:
        assert {"review_id", "kind", "image_url", "description", "observed_at", "status"} <= set(x)
        assert x["status"] == "open"


def test_ac_api_12_every_review_item_has_image_url(client, db):
    e = make_entity(db, label="u", status="unknown")
    make_review(db, entity_id=e, image_uri=f"s3://{BUCKET}/reviews/a.jpg")
    make_review(db, entity_id=e, image_uri=f"s3://{BUCKET}/reviews/b.jpg")
    urls = sorted(x["image_url"] for x in ok(client.get("/review")))
    assert urls == ["/media/reviews/a.jpg", "/media/reviews/b.jpg"]


def test_ac_api_12_candidates_expanded_as_items(client, db):
    c1 = make_entity(db, label="drill A", catalog_id="drill-pistol", status="confirmed", last_location="LAB1S2")
    c2 = make_entity(db, label="drill B", catalog_id="drill-pistol", status="confirmed", last_location="NOD2D4")
    e = make_entity(db, label="maybe drill", status="provisional", needs_review=True)
    make_review(db, entity_id=e, kind="possible_duplicate", candidate_ids=[c1, c2])
    item = ok(client.get("/review"))[0]
    assert sorted(item["candidate_ids"]) == sorted([c1, c2])
    cands = {c["entity_id"]: c for c in item["candidates"]}
    assert set(cands) == {c1, c2}
    for c in cands.values():
        assert {"entity_id", "kind", "status", "label", "confidence"} <= set(c)
    assert cands[c1]["label"] == "drill A"
    assert cands[c2]["last_location"] == "NOD2D4"


def test_ac_api_12_filters_status_kind_and_limit(client, db):
    e = make_entity(db, label="u", status="unknown")
    for i in range(3):
        make_review(db, entity_id=e, kind="unknown_object", created_at=ts(i), observed_at=ts(i))
    dup = make_review(db, entity_id=e, kind="possible_duplicate", created_at=ts(10), observed_at=ts(10))
    done = make_review(db, entity_id=e, kind="unknown_object", status="resolved")
    assert [x["review_id"] for x in ok(client.get("/review", params={"kind": "possible_duplicate"}))] == [dup]
    assert [x["review_id"] for x in ok(client.get("/review", params={"status": "resolved"}))] == [done]
    assert len(ok(client.get("/review", params={"limit": 2}))) == 2


def test_ac_api_12_invalid_filter_values_are_422(client, db):
    assert client.get("/review", params={"status": "bogus"}).status_code == 422
    assert client.get("/review", params={"kind": "bogus"}).status_code == 422


# ------------------------------------------------------------------ AC-API-13
@pytest.fixture()
def open_review(db):
    e = make_entity(db, label="white bag thing", status="unknown", needs_review=True, review_reason="unknown_object",
                    last_seen_at=ts(1), last_location="PMM1D2")
    r = make_review(db, entity_id=e, kind="unknown_object", observed_at=ts(1), location_id="PMM1D2")
    return {"entity": e, "review": r}


def _resolve(client, review_id, **body):
    body.setdefault("operator", "amy")
    return client.post(f"/review/{review_id}/resolve", json=body)


def _review_row(db, rid):
    return db.execute("SELECT status, resolved_by, resolved_at, resolution FROM review_item WHERE review_id=%s",
                      (rid,)).fetchone()


def _entity_row(db, eid):
    return db.execute("SELECT status, catalog_id, label, needs_review, merged_into FROM entity WHERE entity_id=%s",
                      (eid,)).fetchone()


def _assert_resolved(db, rid, status, operator="amy"):
    st, by, at, resolution = _review_row(db, rid)
    assert st == status
    assert by == operator
    assert at is not None
    assert resolution is not None


def test_ac_api_13_assign_catalog(client, db, open_review):
    r = _resolve(client, open_review["review"], action="assign_catalog", catalog_id="ctb-half")
    assert r.status_code == 200, r.text
    status, cat, label, needs_review, _ = _entity_row(db, open_review["entity"])
    assert (status, cat, label, needs_review) == ("confirmed", "ctb-half", "Cargo Transfer Bag (half)", False)
    _assert_resolved(db, open_review["review"], "resolved")


def test_ac_api_13_assign_catalog_with_explicit_label(client, db, open_review):
    r = _resolve(client, open_review["review"], action="assign_catalog", catalog_id="ctb-half",
                 label="CTB with science samples")
    assert r.status_code == 200
    assert _entity_row(db, open_review["entity"])[2] == "CTB with science samples"


def test_ac_api_13_new_catalog_creates_and_assigns(client, db, open_review):
    new = {"catalog_id": "glove-box-bag", "class_name": "glovebox bag", "display_name": "Glovebox bag",
           "synonyms": ["GB bag"], "category": "container"}
    r = _resolve(client, open_review["review"], action="new_catalog", new_catalog=new)
    assert r.status_code == 200, r.text
    cat = db.execute("SELECT class_name, display_name, category FROM catalog_item WHERE catalog_id='glove-box-bag'"
                     ).fetchone()
    assert cat == ("glovebox bag", "Glovebox bag", "container")
    status, cat_id, label, needs_review, _ = _entity_row(db, open_review["entity"])
    assert (status, cat_id, label, needs_review) == ("confirmed", "glove-box-bag", "Glovebox bag", False)
    _assert_resolved(db, open_review["review"], "resolved")


def test_ac_api_13_new_catalog_with_bad_id_is_422_and_nothing_written(client, db, open_review):
    before = table_counts(db)
    r = _resolve(client, open_review["review"], action="new_catalog",
                 new_catalog={"catalog_id": "Bad Id!", "class_name": "x", "display_name": "X"})
    assert r.status_code == 422
    assert table_counts(db) == before
    assert _review_row(db, open_review["review"])[0] == "open"


def test_ac_api_13_merge_into(client, db, open_review):
    target = make_entity(db, label="CTB 1", status="confirmed", catalog_id="ctb-half", last_seen_at=ts(-10))
    r = _resolve(client, open_review["review"], action="merge_into", target_entity_id=target)
    assert r.status_code == 200, r.text
    status, _, _, needs_review, merged_into = _entity_row(db, open_review["entity"])
    assert status == "merged"
    assert str(merged_into) == target
    assert needs_review is False
    # AC-API-9 semantics: target's last_seen advances to the newer source
    assert db.execute("SELECT last_seen_at FROM entity WHERE entity_id=%s", (target,)).fetchone()[0] == ts(1)
    assert len(audit_rows(db, "merge")) == 1
    _assert_resolved(db, open_review["review"], "resolved")


def test_ac_api_13_merge_into_invalid_target_is_409(client, db, open_review):
    person = make_entity(db, label="crew", kind="person")
    r = _resolve(client, open_review["review"], action="merge_into", target_entity_id=person)
    assert r.status_code == 409
    assert _review_row(db, open_review["review"])[0] == "open"


def test_ac_api_13_dismiss(client, db, open_review):
    r = _resolve(client, open_review["review"], action="dismiss", note="lens glare")
    assert r.status_code == 200
    status, _, _, needs_review, _ = _entity_row(db, open_review["entity"])
    assert status == "dismissed"
    assert needs_review is False
    _assert_resolved(db, open_review["review"], "dismissed")


@pytest.mark.parametrize("action,extra", [
    ("assign_catalog", {"catalog_id": "ctb-half"}),
    ("new_catalog", {"new_catalog": {"catalog_id": "foo-bag", "class_name": "foo", "display_name": "Foo"}}),
    ("merge_into", "TARGET"),
    ("dismiss", {}),
])
def test_ac_api_13_every_action_writes_audit_with_operator(client, db, open_review, action, extra):
    if extra == "TARGET":
        extra = {"target_entity_id": make_entity(db, label="t", status="confirmed")}
    before = len(audit_rows(db))
    r = _resolve(client, open_review["review"], action=action, operator="carol", **extra)
    assert r.status_code == 200, r.text
    rows = audit_rows(db)[before:]
    assert rows, "an audit_log row is required"
    assert all(row[2] == "carol" for row in rows)
    assert any(open_review["entity"] in [str(x) for x in row[1]] for row in rows)
    _st, by, _at, resolution = _review_row(db, open_review["review"])
    assert by == "carol"
    assert resolution.get("action") == action


@pytest.mark.parametrize("prior", ["resolved", "dismissed"])
def test_ac_api_13_already_resolved_is_409(client, db, prior):
    e = make_entity(db, label="x", status="confirmed")
    rid = make_review(db, entity_id=e, status=prior)
    before = table_counts(db)
    r = _resolve(client, rid, action="dismiss")
    assert r.status_code == 409
    assert table_counts(db) == before
    assert _entity_row(db, e)[0] == "confirmed"


def test_ac_api_13_second_resolve_is_409(client, db, open_review):
    assert _resolve(client, open_review["review"], action="dismiss").status_code == 200
    assert _resolve(client, open_review["review"], action="assign_catalog", catalog_id="ctb-half").status_code == 409
    assert _entity_row(db, open_review["entity"])[0] == "dismissed"


@pytest.mark.parametrize("body", [
    {"action": "assign_catalog"},                       # missing catalog_id
    {"action": "new_catalog"},                          # missing new_catalog
    {"action": "merge_into"},                           # missing target
    {"action": "explode"},                              # unknown action
    {},                                                 # missing action
    {"action": "assign_catalog", "catalog_id": "no-such-catalog"},
])
def test_ac_api_13_missing_or_invalid_payload_is_422(client, db, open_review, body):
    before = table_counts(db)
    r = client.post(f"/review/{open_review['review']}/resolve", json=dict(body, operator="amy"))
    assert r.status_code == 422, r.text
    assert table_counts(db) == before
    assert _review_row(db, open_review["review"])[0] == "open"


def test_ac_api_13_missing_operator_is_422(client, db, open_review):
    r = client.post(f"/review/{open_review['review']}/resolve", json={"action": "dismiss"})
    assert r.status_code == 422


def test_ac_api_13_unknown_review_is_404(client, db):
    r = _resolve(client, str(uuid.uuid4()), action="dismiss")
    assert r.status_code == 404


def test_ac_api_13_resolved_review_leaves_open_queue(client, db, open_review):
    assert _resolve(client, open_review["review"], action="dismiss").status_code == 200
    assert ok(client.get("/review")) == []
    resolved = ok(client.get("/review", params={"status": "dismissed"}))
    assert [x["review_id"] for x in resolved] == [open_review["review"]]
    assert parse_dt(resolved[0]["observed_at"]) == ts(1)
