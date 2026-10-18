"""UI review round 6 (docs/ui-review/round6/findings.json):

AC-API-46 (UI6-05) structure memory: the operator dismissed "insulated hose" (review 6388e99f) as Not inventory and the
same duct came back 10 minutes later as "white padded tube". A not_inventory dismissal is now remembered (label key,
description tokens, place, exemplar embedding) and listed for perception by GET /structure-memory.

AC-API-47 (UI6-08) "Laptop moved from Destiny (LAB1) to US Orbital Segment": a move to the place's own parent (or
inside it) is a loss of precision, not a move, and is not stored."""
from __future__ import annotations

from pathlib import Path

import psycopg
import pytest

from tests.conftest import (
    CONTRACTS_DB,
    iso,
    make_entity,
    make_exemplar,
    make_review,
    make_sighting,
    ts,
    vec,
)
from tests.test_ac_api_18_19_23_ingest import payload

MIGRATION = CONTRACTS_DB / "007_structure_memory.sql"
HOSE_DESC = "a thick white insulated hose running along the wall of the module"


def _hose(db, *, location="LAB1", catalog_id=None, status="unknown"):
    e = make_entity(db, label="insulated hose", status=status, catalog_id=catalog_id, description=HOSE_DESC,
                    last_location=location, last_seen_at=ts(0))
    make_sighting(db, e, seen_at=ts(0), location_id=location, description="white insulated hose behind the rack")
    r = make_review(db, entity_id=e, kind="unknown_object", location_id=location, model_guess="insulated hose",
                    description=f"insulated hose: {HOSE_DESC} (not in the catalog)")
    return e, r


def _resolve(client, review_id, **body):
    body.setdefault("operator", "ui-review-6")
    return client.post(f"/review/{review_id}/resolve", json=body)


def _memory(client, **params):
    r = client.get("/structure-memory", params=params)
    assert r.status_code == 200, r.text
    return r.json()


# ================================================================================ AC-API-46 structure memory
def test_migration_007_exists_and_is_idempotent(database_url):
    assert MIGRATION.is_file()
    sql = MIGRATION.read_text()
    with psycopg.connect(database_url, autocommit=True) as conn:
        conn.execute(sql)
        conn.execute(sql)  # a restart re-applies every migration
        cols = {r[0] for r in conn.execute(
            "SELECT column_name FROM information_schema.columns WHERE table_name = 'structure_memory'")}
    assert {"memory_id", "review_id", "entity_id", "label", "label_key", "description_tokens", "location_id",
            "module_id", "embedding", "created_by", "created_at"} <= cols


def test_live_case_not_inventory_dismissal_is_remembered(client, db):
    e, r = _hose(db)
    s = make_sighting(db, e, seen_at=ts(1), location_id="LAB1")
    for hot, q in ((3, 0.4), (4, 0.9)):
        x = make_exemplar(db, e, vec((hot, 1.0)), quality=q)
        db.execute("UPDATE entity_exemplar SET sighting_id = %s WHERE exemplar_id = %s", (s, x))
    resp = _resolve(client, r, action="dismiss", note="not_inventory")
    assert resp.status_code == 200, resp.text
    [m] = _memory(client)
    assert m["review_id"] == r and m["entity_id"] == e
    assert m["label"] == "insulated hose" and m["label_key"] == "insulatedhose"
    assert m["location_id"] == "LAB1" and m["module_id"] == "LAB1"
    for word in ("insulated", "hose", "thick", "white", "running", "wall", "rack"):
        assert word in m["description_tokens"]
    assert "the" not in m["description_tokens"] and "a" not in m["description_tokens"]
    assert len(m["embedding"]) == 768
    assert m["embedding"][4] == pytest.approx(1.0, abs=1e-3)  # the best-quality exemplar
    assert m["created_by"] == "ui-review-6"
    audit = db.execute("SELECT detail FROM audit_log WHERE op = 'dismiss' ORDER BY audit_id DESC LIMIT 1").fetchone()[0]
    assert audit["structure_memory_id"] == m["memory_id"]


def test_note_is_matched_trimmed_and_case_insensitively(client, db):
    _e, r = _hose(db)
    assert _resolve(client, r, action="dismiss", note="  Not_Inventory ").status_code == 200
    assert len(_memory(client)) == 1


@pytest.mark.parametrize("body", [
    {"action": "dismiss"},
    {"action": "dismiss", "note": "duplicate"},
    {"action": "assign_catalog", "catalog_id": "laptop", "note": "not_inventory"},
])
def test_other_resolutions_write_no_memory(client, db, body):
    _e, r = _hose(db)
    assert _resolve(client, r, **body).status_code == 200
    assert _memory(client) == []


def test_catalogued_entity_writes_no_memory(client, db):
    _e, r = _hose(db, catalog_id="power-cable", status="provisional")
    assert _resolve(client, r, action="dismiss", note="not_inventory").status_code == 200
    assert _memory(client) == []


def test_segment_level_review_has_no_module_and_no_exemplar_gives_null_embedding(client, db):
    _e, r = _hose(db, location="USOS")
    assert _resolve(client, r, action="dismiss", note="not_inventory").status_code == 200
    [m] = _memory(client)
    assert m["location_id"] == "USOS" and m["module_id"] is None and m["embedding"] is None


def test_zone_review_rolls_up_to_its_module(client, db):
    _e, r = _hose(db, location="LAB1S2")
    _resolve(client, r, action="dismiss", note="not_inventory")
    [m] = _memory(client)
    assert (m["location_id"], m["module_id"]) == ("LAB1S2", "LAB1")


def test_review_without_location_uses_the_entity_place(client, db):
    e = make_entity(db, label="grey device", status="unknown", last_location="ZVEZDA", last_seen_at=ts(0))
    r = make_review(db, entity_id=e, kind="unknown_object", location_id=None)
    _resolve(client, r, action="dismiss", note="not_inventory")
    [m] = _memory(client)
    assert (m["location_id"], m["module_id"]) == ("ZVEZDA", "ZVEZDA")


def test_review_person_words_are_not_remembered(client, db):
    e, r = _hose(db)
    make_sighting(db, e, seen_at=ts(2), location_id="LAB1",
                  description="hose next to a crew member with dark hair wearing a blue shirt")
    _resolve(client, r, action="dismiss", note="not_inventory")
    tokens = _memory(client)[0]["description_tokens"]
    for word in ("crew", "member", "hair", "wearing", "shirt"):
        assert word not in tokens


def test_review_exemplar_without_a_known_sighting_is_not_remembered(client, db):
    e, r = _hose(db)
    make_exemplar(db, e, vec((4, 1.0)), quality=0.9)  # legacy: no sighting link, person_overlap unknown
    _resolve(client, r, action="dismiss", note="not_inventory")
    assert _memory(client)[0]["embedding"] is None


def test_review_memory_is_not_reactivated_by_a_later_dismissal_for_another_reason(client, db):
    e, r = _hose(db)
    _resolve(client, r, action="dismiss", note="not_inventory")
    db.execute("UPDATE entity SET status = 'unknown' WHERE entity_id = %s", (e,))
    r2 = make_review(db, entity_id=e, kind="possible_duplicate", location_id="LAB1")
    assert _resolve(client, r2, action="dismiss", note="duplicate").status_code == 200
    assert _memory(client) == []


def test_person_overlap_exemplar_is_never_remembered(client, db):
    e, r = _hose(db)
    s = make_sighting(db, e, seen_at=ts(1), location_id="LAB1")
    db.execute("UPDATE sighting SET person_overlap = TRUE WHERE sighting_id = %s", (s,))
    x = make_exemplar(db, e, vec((7, 1.0)), quality=0.99)
    db.execute("UPDATE entity_exemplar SET sighting_id = %s WHERE exemplar_id = %s", (s, x))
    _resolve(client, r, action="dismiss", note="not_inventory")
    assert _memory(client)[0]["embedding"] is None


def test_restored_or_classified_entity_withdraws_the_memory(client, db):
    e, r = _hose(db)
    _resolve(client, r, action="dismiss", note="not_inventory")
    assert len(_memory(client)) == 1
    db.execute("UPDATE entity SET status = 'unknown' WHERE entity_id = %s", (e,))
    assert _memory(client) == []


def test_deleted_entity_keeps_the_memory(client, db):
    e, r = _hose(db)
    _resolve(client, r, action="dismiss", note="not_inventory")
    db.execute("DELETE FROM review_item WHERE entity_id = %s", (e,))
    db.execute("DELETE FROM entity WHERE entity_id = %s", (e,))
    [m] = _memory(client)
    assert m["entity_id"] is None and m["label_key"] == "insulatedhose"


def test_listing_is_newest_first_and_limited(client, db):
    ids = []
    for i in range(3):
        e = make_entity(db, label=f"duct {i}", status="unknown", last_location="LAB1", last_seen_at=ts(i))
        r = make_review(db, entity_id=e, location_id="LAB1")
        _resolve(client, r, action="dismiss", note="not_inventory")
        ids.append(r)
    assert [m["review_id"] for m in _memory(client)] == ids[::-1]
    assert len(_memory(client, limit=2)) == 2
    assert client.get("/structure-memory", params={"limit": 0}).status_code == 422
    assert client.get("/structure-memory", params={"limit": 1001}).status_code == 422


def test_tokens_are_capped_and_letters_only(client, db):
    long_desc = " ".join(f"word{chr(97 + i % 26)}{chr(97 + i // 26)}x" for i in range(80)) + " 1234 a-b x9"
    e = make_entity(db, label="cover", status="unknown", description=long_desc, last_location="LAB1",
                    last_seen_at=ts(0))
    r = make_review(db, entity_id=e, location_id="LAB1")
    _resolve(client, r, action="dismiss", note="not_inventory")
    tokens = _memory(client)[0]["description_tokens"]
    assert len(tokens) <= 40 and all(t.isalpha() and len(t) >= 3 for t in tokens)


def test_openapi_contract_lists_the_endpoint():
    spec = (Path(CONTRACTS_DB).parent / "openapi" / "inventory-api.yaml").read_text()
    assert "/structure-memory:" in spec and "StructureMemory:" in spec


# ================================================================================ AC-API-47 no move inside one place
def _move(frm, to, source="pipeline"):
    body = payload(reviews=[], decisions=[])
    body["events"] = [{"event_type": "moved", "entity_ref": "o1", "actor_desc": None, "from_location": frm,
                       "to_location": to, "occurred_at": iso(10), "confidence": 0.7, "evidence_uri": None,
                       "source": source}]
    return body


@pytest.mark.parametrize(("frm", "to"), [("LAB1", "USOS"), ("LAB1S2", "LAB1"), ("LAB1", "LAB1"), ("LAB1S2", "USOS")])
def test_live_case_move_to_own_parent_or_inside_is_not_stored(client, db, frm, to):
    r = client.post("/ingest/window", json=_move(frm, to))
    assert r.status_code == 201, r.text
    assert db.execute("SELECT count(*) FROM event WHERE event_type = 'moved'").fetchone()[0] == 0
    assert db.execute("SELECT count(*) FROM sighting").fetchone()[0] == 1  # the rest of the window is written
    detail = db.execute("SELECT detail FROM audit_log WHERE op = 'update' ORDER BY audit_id DESC LIMIT 1").fetchone()[0]
    assert detail["moved_events_skipped"] == 1 and detail["event_ids"] == []


@pytest.mark.parametrize(("frm", "to"), [("LAB1", "NOD1"), ("LAB1S2", "LAB1P3"), ("USOS", "ROS"), (None, "LAB1"),
                                         ("LAB1", None), ("USOS", "LAB1"), ("LAB1", "LAB1S2")])  # review: refinements
def test_real_moves_are_stored(client, db, frm, to):
    assert client.post("/ingest/window", json=_move(frm, to)).status_code == 201
    assert db.execute("SELECT count(*) FROM event WHERE event_type = 'moved'").fetchone()[0] == 1


def test_speech_move_inside_one_place_is_skipped_too(client, db):
    assert client.post("/ingest/window", json=_move("LAB1S2", "LAB1", source="speech")).status_code == 201
    assert db.execute("SELECT count(*) FROM event").fetchone()[0] == 0


def test_other_event_types_are_unaffected(client, db):
    body = _move("LAB1", "USOS")
    body["events"][0]["event_type"] = "used"
    assert client.post("/ingest/window", json=body).status_code == 201
    assert db.execute("SELECT count(*) FROM event WHERE event_type = 'used'").fetchone()[0] == 1
