"""AC-API-9: POST /items/{id}/merge."""
from __future__ import annotations

import json
import uuid

import pytest

from tests.conftest import audit_rows, make_entity, make_event, make_exemplar, make_sighting, ok, table_counts, ts, vec


@pytest.fixture()
def pair(db):
    target = make_entity(db, label="drill", status="confirmed", catalog_id="drill-pistol", last_location="LAB1S2",
                         last_seen_at=ts(10))
    source = make_entity(db, label="drill (dup)", status="provisional", last_location="NOD2D4", last_seen_at=ts(20))
    s1 = make_sighting(db, source, seen_at=ts(15), location_id="NOD2D4")
    s2 = make_sighting(db, source, seen_at=ts(20), location_id="NOD2D4")
    ev = make_event(db, source, event_type="moved", occurred_at=ts(19), from_location="LAB1S2", to_location="NOD2D4")
    ex = make_exemplar(db, source, vec((0, 1.0)))
    make_sighting(db, target, seen_at=ts(10), location_id="LAB1S2")
    return {"target": target, "source": source, "sightings": [s1, s2], "events": [ev], "exemplars": [ex]}


def _merge(client, source, target, operator="amy"):
    return client.post(f"/items/{source}/merge", json={"into_entity_id": target, "operator": operator,
                                                       "note": "same drill"})


def test_ac_api_09_source_marked_merged_into_target(client, db, pair):
    r = _merge(client, pair["source"], pair["target"])
    assert r.status_code == 200, r.text
    status, merged_into = db.execute("SELECT status, merged_into FROM entity WHERE entity_id=%s",
                                     (pair["source"],)).fetchone()
    assert status == "merged"
    assert str(merged_into) == pair["target"]


def test_ac_api_09_repoints_sightings_events_exemplars(client, db, pair):
    assert _merge(client, pair["source"], pair["target"]).status_code == 200
    src = pair["source"]
    for table in ("sighting", "event", "entity_exemplar"):
        assert db.execute(f"SELECT count(*) FROM {table} WHERE entity_id=%s", (src,)).fetchone()[0] == 0, table
    tgt = pair["target"]
    assert db.execute("SELECT count(*) FROM sighting WHERE entity_id=%s", (tgt,)).fetchone()[0] == 3
    assert db.execute("SELECT count(*) FROM event WHERE entity_id=%s", (tgt,)).fetchone()[0] == 1
    assert db.execute("SELECT count(*) FROM entity_exemplar WHERE entity_id=%s", (tgt,)).fetchone()[0] == 1


def test_ac_api_09_target_last_seen_updated_when_source_newer(client, db, pair):
    assert _merge(client, pair["source"], pair["target"]).status_code == 200
    last_seen = db.execute("SELECT last_seen_at FROM entity WHERE entity_id=%s", (pair["target"],)).fetchone()[0]
    assert last_seen == ts(20)


def test_ac_api_09_target_last_seen_kept_when_source_older(client, db):
    target = make_entity(db, label="t", last_location="LAB1S2", last_seen_at=ts(50))
    source = make_entity(db, label="s", last_location="NOD2D4", last_seen_at=ts(5))
    assert _merge(client, source, target).status_code == 200
    row = db.execute("SELECT last_seen_at, last_location FROM entity WHERE entity_id=%s", (target,)).fetchone()
    assert row == (ts(50), "LAB1S2")


def test_ac_api_09_audit_has_enough_detail_to_undo(client, db, pair):
    assert _merge(client, pair["source"], pair["target"], operator="bob").status_code == 200
    rows = audit_rows(db, "merge")
    assert len(rows) == 1
    _op, entity_ids, actor, detail, _at = rows[0]
    assert actor == "bob"
    assert {str(x) for x in entity_ids} == {pair["source"], pair["target"]}
    blob = json.dumps(detail)
    for sid in pair["sightings"]:
        assert str(sid) in blob, "moved sighting ids must be recorded"
    for eid in pair["events"]:
        assert str(eid) in blob, "moved event ids must be recorded"
    for xid in pair["exemplars"]:
        assert str(xid) in blob, "moved exemplar ids must be recorded"
    assert "provisional" in blob, "source's previous status must be recorded"
    assert pair["source"] in blob and pair["target"] in blob


def test_ac_api_09_merge_into_self_is_409(client, db, pair):
    before = table_counts(db)
    r = _merge(client, pair["source"], pair["source"])
    assert r.status_code == 409
    assert table_counts(db) == before


def test_ac_api_09_merge_into_merged_entity_is_409(client, db, pair):
    other = make_entity(db, label="other")
    already = make_entity(db, label="gone", status="merged", merged_into=other)
    r = _merge(client, pair["source"], already)
    assert r.status_code == 409
    assert db.execute("SELECT status FROM entity WHERE entity_id=%s", (pair["source"],)).fetchone()[0] == "provisional"


def test_ac_api_09_merging_an_already_merged_source_is_409(client, db, pair):
    other = make_entity(db, label="other")
    already = make_entity(db, label="gone", status="merged", merged_into=other)
    r = _merge(client, already, pair["target"])
    assert r.status_code == 409


def test_ac_api_09_merge_across_kinds_is_409(client, db):
    obj = make_entity(db, label="camera", kind="object")
    person = make_entity(db, label="crew member", kind="person")
    before = table_counts(db)
    r = _merge(client, person, obj)
    assert r.status_code == 409
    assert table_counts(db) == before
    assert db.execute("SELECT status FROM entity WHERE entity_id=%s", (person,)).fetchone()[0] == "provisional"


@pytest.mark.parametrize("which", ["source", "target"])
def test_ac_api_09_unknown_entity_is_404(client, db, pair, which):
    ghost = str(uuid.uuid4())
    src, tgt = (ghost, pair["target"]) if which == "source" else (pair["source"], ghost)
    r = _merge(client, src, tgt)
    assert r.status_code == 404


def test_ac_api_09_missing_into_entity_id_is_422(client, db, pair):
    r = client.post(f"/items/{pair['source']}/merge", json={"operator": "amy"})
    assert r.status_code == 422


def test_ac_api_09_merged_source_hidden_from_default_list(client, db, pair):
    assert _merge(client, pair["source"], pair["target"]).status_code == 200
    listed = [i["entity_id"] for i in ok(client.get("/items"))["items"]]
    assert pair["source"] not in listed
    assert pair["target"] in listed
