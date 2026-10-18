"""AC-API-33..38: contract additions requested by the second live UI review (docs/ui-review/round2).

UI2-05 unmerge + merged_into/merged_from, UI2-06 ranked duplicate suggestions (score, match_reasons), UI-16
case-insensitive label sort, UI2-07 bbox_source (review image, item sightings, item snapshot), UI2-09/UI-13 event
location_role and actor_kind. Migration 004 is tested on a database that already holds data.
"""

from __future__ import annotations

import uuid

import psycopg
import pytest

from inventory_api.migrations import apply_migrations
from tests.conftest import (
    ADMIN_URL,
    BUCKET,
    CONTRACTS_DB,
    _url_for,
    audit_rows,
    iso,
    make_entity,
    make_event,
    make_exemplar,
    make_review,
    make_sighting,
    ok,
    parse_dt,
    ts,
    vec,
)
from tests.test_ac_api_18_19_23_ingest import CROP, FRAME, payload

FRAME2 = f"s3://{BUCKET}/frames/cam-lab1-fwd/0002.jpg"


def _merge(client, source, target, operator="amy"):
    return client.post(f"/items/{source}/merge", json={"into_entity_id": target, "operator": operator})


def _unmerge(client, source, operator="amy", note=None):
    body = {"operator": operator}
    if note is not None:
        body["note"] = note
    return client.post(f"/items/{source}/unmerge", json=body)


def _snapshot(db):
    """Everything a merge touches (and an unmerge must restore)."""
    return {
        "entity": db.execute(
            "SELECT entity_id, status, merged_into, last_seen_at, last_location, first_seen_at, needs_review, "
            "review_reason, parent_entity, location_set_at, location_set_by FROM entity ORDER BY entity_id"
        ).fetchall(),
        "sighting": db.execute("SELECT sighting_id, entity_id FROM sighting ORDER BY 1").fetchall(),
        "event": db.execute("SELECT event_id, entity_id, actor_entity FROM event ORDER BY 1").fetchall(),
        "exemplar": db.execute(
            "SELECT exemplar_id, entity_id, quality, crop_uri, embedding::text FROM entity_exemplar ORDER BY 1"
        ).fetchall(),
        "review": db.execute(
            "SELECT review_id, entity_id, candidate_ids, status, resolution, resolved_by, resolved_at "
            "FROM review_item ORDER BY 1"
        ).fetchall(),
    }


def _rich_world(db):
    target = make_entity(
        db, label="drill", status="confirmed", last_location="LAB1S2", last_seen_at=ts(10), first_seen_at=ts(5)
    )
    source = make_entity(
        db, label="drill dup", last_location="NOD2D4", last_seen_at=ts(20), first_seen_at=ts(1),
        needs_review=True, review_reason="possible_duplicate",
    )
    db.execute("UPDATE entity SET location_set_at=last_seen_at, location_set_by='pipeline'")
    make_sighting(db, source, seen_at=ts(20), location_id="NOD2D4")
    make_sighting(db, target, seen_at=ts(10), location_id="LAB1S2")
    make_event(db, source, occurred_at=ts(19), from_location="LAB1S2", to_location="NOD2D4")
    for i in range(12):
        make_exemplar(db, target, vec((i, 1.0)), quality=0.4 + i / 100, crop_uri=f"s3://{BUCKET}/t{i}.jpg")
        make_exemplar(db, source, vec((50 + i, 1.0)), quality=0.3 + i / 50)
    child = make_entity(db, label="inside")
    db.execute("UPDATE entity SET parent_entity=%s WHERE entity_id=%s", (source, child))
    crew = make_entity(db, label="crew", kind="person")
    ev = make_event(db, child, occurred_at=ts(3))
    db.execute("UPDATE event SET actor_entity=%s WHERE event_id=%s", (source, ev))
    other = make_entity(db, label="o")
    own_review = make_review(db, entity_id=source)
    cand_review = make_review(db, entity_id=other, kind="possible_duplicate", candidate_ids=[source, target])
    return {"target": target, "source": source, "child": child, "crew": crew, "own_review": own_review,
            "cand_review": cand_review}


# ====================================================================== AC-API-33 unmerge
def test_ac_api_33_unmerge_restores_the_pre_merge_state(client, db):
    w = _rich_world(db)
    before = _snapshot(db)
    assert _merge(client, w["source"], w["target"]).status_code == 200
    assert _snapshot(db) != before
    body = ok(_unmerge(client, w["source"], note="not the same drill"))
    assert _snapshot(db) == before
    assert body["unmerged_entity_id"] == w["source"]
    assert body["target_entity_id"] == w["target"]
    assert body["source"]["entity_id"] == w["source"] and body["source"]["status"] == "provisional"
    assert body["target"]["entity_id"] == w["target"]
    restored = body["restored"]
    assert len(restored["sighting_ids"]) == 1
    # 24 exemplars after the merge: the cap dropped the 4 weakest (source) ones, which come back from the audit
    assert len(restored["exemplar_ids"]) == 8 and len(restored["reinserted_exemplar_ids"]) == 4
    assert restored["reopened_review_id"] is None


def test_ac_api_33_unmerge_restores_exemplars_dropped_by_the_cap(client, db):
    target = make_entity(db, label="t", last_seen_at=ts(1))
    source = make_entity(db, label="s", last_seen_at=ts(2))
    for i in range(20):
        make_exemplar(db, target, vec((i, 1.0)), quality=0.50 + i / 100)
        make_exemplar(db, source, vec((100 + i, 1.0)), quality=0.10 + i / 25)
    before = _snapshot(db)
    assert _merge(client, source, target).status_code == 200
    ok(_unmerge(client, source))
    assert _snapshot(db) == before


def test_ac_api_33_unmerge_of_a_review_merge_reopens_the_review(client, db):
    target = make_entity(db, label="bag", status="confirmed", last_location="NOD1D1", last_seen_at=ts(1))
    source = make_entity(db, label="bag", needs_review=True, review_reason="possible_duplicate",
                         last_location="NOD1D1", last_seen_at=ts(2))
    rv = make_review(db, entity_id=source, kind="possible_duplicate", candidate_ids=[target])
    before = _snapshot(db)
    r = client.post(f"/review/{rv}/resolve",
                    json={"action": "merge_into", "target_entity_id": target, "operator": "ui-review-2"})
    assert r.status_code == 200, r.text
    body = ok(_unmerge(client, source, operator="ui-review-2"))
    assert body["restored"]["reopened_review_id"] == rv
    assert _snapshot(db) == before
    review = ok(client.get(f"/review/{rv}"))
    assert review["status"] == "open" and review["resolution"] is None


def test_ac_api_33_unmerge_writes_an_attributed_audit_row(client, db):
    w = _rich_world(db)
    assert _merge(client, w["source"], w["target"]).status_code == 200
    ok(_unmerge(client, w["source"], operator="bob", note="wrong merge"))
    row = audit_rows(db, "split")[-1]
    _op, entity_ids, actor, detail, _at = row
    assert actor == "bob"
    assert [str(e) for e in entity_ids] == [w["source"], w["target"]]
    assert detail["action"] == "unmerge" and detail["note"] == "wrong merge"
    merge_audit_id = db.execute("SELECT audit_id FROM audit_log WHERE op='merge'").fetchone()[0]
    assert detail["merge_audit_id"] == merge_audit_id
    assert detail["source_entity_id"] == w["source"] and detail["target_entity_id"] == w["target"]


def test_ac_api_33_unmerge_keeps_an_operator_move_of_the_target_made_after_the_merge(client, db):
    w = _rich_world(db)
    assert _merge(client, w["source"], w["target"]).status_code == 200
    ok(client.patch(f"/items/{w['target']}", json={"last_location": "PMM1D2", "operator": "amy"}))
    ok(_unmerge(client, w["source"]))
    loc, by, last_seen = db.execute(
        "SELECT last_location, location_set_by, last_seen_at FROM entity WHERE entity_id=%s", (w["target"],)
    ).fetchone()
    assert (loc, by) == ("PMM1D2", "amy")  # the newer operator edit is not clobbered
    assert last_seen == ts(10)  # the merge-made last_seen (source's ts 20) is undone


def test_ac_api_33_unmerge_409_when_the_target_was_merged_again(client, db):
    w = _rich_world(db)
    third = make_entity(db, label="drill 3", last_seen_at=ts(30))
    assert _merge(client, w["source"], w["target"]).status_code == 200
    assert _merge(client, w["target"], third).status_code == 200
    before = _snapshot(db)
    r = _unmerge(client, w["source"])
    assert r.status_code == 409
    assert "merged" in r.json()["detail"]
    assert _snapshot(db) == before
    assert audit_rows(db, "split") == []


def _sight_target(client, target, embedding, minutes):
    body = payload(events=[], reviews=[], decisions=[])
    a = body["assignments"][0]
    del a["new_entity"]
    a["entity_id"] = target
    a["sighting"]["seen_at"] = iso(minutes)
    a["exemplar"]["embedding"] = embedding
    body["window"]["t_start"] = body["window"]["t_end"] = iso(minutes)
    assert client.post("/ingest/window", json=body).status_code == 201


def test_ac_api_33_unmerge_409_when_new_target_sightings_came_from_source_exemplars(client, db):
    target = make_entity(db, label="t", last_location="LAB1S2", last_seen_at=ts(1))
    source = make_entity(db, label="s", last_location="LAB1S2", last_seen_at=ts(2))
    make_exemplar(db, target, vec((0, 1.0)))
    make_exemplar(db, source, vec((7, 1.0)))
    assert _merge(client, source, target).status_code == 200
    _sight_target(client, target, vec((7, 1.0), (0, 0.1)), 30)  # looks like the source
    before = _snapshot(db)
    r = _unmerge(client, source)
    assert r.status_code == 409
    assert "sighting" in r.json()["detail"]
    assert _snapshot(db) == before


def test_ac_api_33_new_target_sightings_from_target_exemplars_stay_on_the_target(client, db):
    target = make_entity(db, label="t", last_location="LAB1S2", last_seen_at=ts(1))
    source = make_entity(db, label="s", last_location="NOD1D1", last_seen_at=ts(2))
    make_exemplar(db, target, vec((0, 1.0)))
    make_exemplar(db, source, vec((7, 1.0)))
    make_sighting(db, source, seen_at=ts(2), location_id="NOD1D1")
    assert _merge(client, source, target).status_code == 200
    _sight_target(client, target, vec((0, 1.0), (7, 0.1)), 30)  # looks like the target
    body = ok(_unmerge(client, source))
    rows = db.execute("SELECT entity_id, seen_at FROM sighting ORDER BY seen_at").fetchall()
    assert [(str(e), t) for e, t in rows] == [(source, ts(2)), (target, ts(30))]
    assert body["target"]["last_seen_at"] is not None and parse_dt(body["target"]["last_seen_at"]) == ts(30)
    assert body["target"]["last_location"] == "LAB1S2"
    # the target's new exemplar stays; the source gets its own back
    assert db.execute("SELECT count(*) FROM entity_exemplar WHERE entity_id=%s", (target,)).fetchone()[0] == 2
    assert db.execute("SELECT count(*) FROM entity_exemplar WHERE entity_id=%s", (source,)).fetchone()[0] == 1


def test_ac_api_33_ingest_that_waited_on_the_merge_lock_still_counts_as_after_the_merge(client, db):
    """A window whose transaction started before the merge committed (it waited on the target's row lock) has an
    older created_at; the merge's sighting watermark still classifies its sighting as post-merge."""
    target = make_entity(db, label="t", last_location="LAB1S2", last_seen_at=ts(1))
    source = make_entity(db, label="s", last_location="LAB1S2", last_seen_at=ts(2))
    make_exemplar(db, target, vec((0, 1.0)))
    make_exemplar(db, source, vec((7, 1.0)))
    assert _merge(client, source, target).status_code == 200
    assert audit_rows(db, "merge")[-1][3]["sighting_watermark"] == 0
    early = db.execute("SELECT now() - interval '1 hour'").fetchone()[0]
    wid = db.execute("INSERT INTO observation_window (camera_id, t_start, t_end, model, created_at) "
                     "VALUES ('cam-lab1-fwd', %s, %s, 'm', %s) RETURNING window_id", (ts(30), ts(30), early)).fetchone()[0]
    make_sighting(db, target, seen_at=ts(30), location_id="LAB1S2", window_id=wid)
    db.execute("INSERT INTO entity_exemplar (entity_id, embedding, created_at) VALUES (%s, %s::halfvec, %s)",
               (target, "[" + ",".join(f"{x:.6f}" for x in vec((7, 1.0), (0, 0.1))) + "]", early))
    assert _unmerge(client, source).status_code == 409


@pytest.mark.parametrize("status", ["confirmed", "dismissed"])
def test_ac_api_33_unmerge_409_when_not_merged(client, db, status):
    e = make_entity(db, label="x", status=status)
    assert _unmerge(client, e).status_code == 409


def test_ac_api_33_unmerge_twice_is_409_and_merge_again_works(client, db):
    w = _rich_world(db)
    assert _merge(client, w["source"], w["target"]).status_code == 200
    ok(_unmerge(client, w["source"]))
    assert _unmerge(client, w["source"]).status_code == 409
    assert _merge(client, w["source"], w["target"]).status_code == 200
    ok(_unmerge(client, w["source"]))


def test_ac_api_33_unmerge_404_and_422(client, db):
    assert _unmerge(client, str(uuid.uuid4())).status_code == 404
    e = make_entity(db, label="x")
    assert client.post(f"/items/{e}/unmerge", json={}).status_code == 422
    assert client.post(f"/items/{e}/unmerge", json={"operator": ""}).status_code == 422
    assert client.post("/items/not-a-uuid/unmerge", json={"operator": "amy"}).status_code == 422


def test_ac_api_33_unmerge_409_when_merge_audit_is_missing(client, db):
    target = make_entity(db, label="t")
    source = make_entity(db, label="s", status="merged", merged_into=target)  # legacy: merged without audit
    r = _unmerge(client, source)
    assert r.status_code == 409
    assert "audit" in r.json()["detail"]


# ====================================================================== AC-API-34 merged_into / merged_from
def test_ac_api_34_merged_into_and_merged_from_round_trip(client, db):
    target = make_entity(db, label="cargo transfer bag", last_location="NOD1D1", last_seen_at=ts(1))
    source = make_entity(db, label="cargo transfer bag (dup)", last_location="NOD1D1", last_seen_at=ts(2))
    live = ok(client.get(f"/items/{source}"))
    assert live["merged_into"] is None and live["merged_at"] is None and live["merged_from"] == []
    assert _merge(client, source, target).status_code == 200
    src = ok(client.get(f"/items/{source}"))
    assert src["merged_into"] == {"entity_id": target, "label": "cargo transfer bag"}
    merged_at = parse_dt(src["merged_at"])
    tgt = ok(client.get(f"/items/{target}"))
    assert tgt["merged_into"] is None and tgt["merged_at"] is None
    assert len(tgt["merged_from"]) == 1
    mf = tgt["merged_from"][0]
    assert mf["entity_id"] == source and mf["label"] == "cargo transfer bag (dup)"
    assert parse_dt(mf["merged_at"]) == merged_at
    listed = ok(client.get("/items", params={"status": "merged"}))["items"]
    assert listed[0]["merged_into"]["entity_id"] == target
    ok(_unmerge(client, source))
    assert ok(client.get(f"/items/{source}"))["merged_into"] is None
    assert ok(client.get(f"/items/{target}"))["merged_from"] == []


def test_ac_api_34_merged_from_lists_every_source_oldest_first(client, db):
    target = make_entity(db, label="t")
    a = make_entity(db, label="a")
    b = make_entity(db, label="b")
    assert _merge(client, a, target).status_code == 200
    assert _merge(client, b, target).status_code == 200
    got = ok(client.get(f"/items/{target}"))["merged_from"]
    assert [m["entity_id"] for m in got] == [a, b]


def test_ac_api_34_legacy_merged_row_without_audit_has_null_merged_at(client, db):
    target = make_entity(db, label="t")
    source = make_entity(db, label="s", status="merged", merged_into=target)
    src = ok(client.get(f"/items/{source}"))
    assert src["merged_into"] == {"entity_id": target, "label": "t"} and src["merged_at"] is None
    assert ok(client.get(f"/items/{target}"))["merged_from"] == [
        {"entity_id": source, "label": "s", "merged_at": None}
    ]


# ====================================================================== AC-API-35 ranked candidates
def _dup(db, subject, candidate_ids=()):
    return make_review(db, entity_id=subject, kind="possible_duplicate", candidate_ids=candidate_ids)


def _cands(client, rid):
    return ok(client.get(f"/review/{rid}"))["candidates"]


def _drill_world(db):
    w = {
        "subj_nod1": make_entity(db, label="drill", catalog_id="drill-pistol", last_location="NOD1D1",
                                 last_seen_at=ts(10), description="black pistol-grip drill with orange battery"),
        "subj_lab": make_entity(db, label="drill", catalog_id="drill-pistol", last_location="LAB1S2",
                                last_seen_at=ts(300), description="yellow cordless drill on the rack"),
        "c_nod1": make_entity(db, label="drill", catalog_id="drill-pistol", last_location="NOD1D1",
                              last_seen_at=ts(12), description="black pistol grip drill, orange battery"),
        "c_lab": make_entity(db, label="drill", catalog_id="drill-pistol", last_location="LAB1P3",
                             last_seen_at=ts(299), description="yellow cordless drill"),
        "c_toy": make_entity(db, label="drill", catalog_id="drill-pistol", last_location="PMM1D2",
                             last_seen_at=ts(2000), description="toy space shuttle"),
    }
    return w


def test_ac_api_35_suggested_candidates_carry_score_and_match_reasons(client, db):
    w = _drill_world(db)
    cands = _cands(client, _dup(db, w["subj_nod1"]))
    assert cands
    for c in cands:
        assert isinstance(c["score"], float)
        assert isinstance(c["match_reasons"], list) and all(isinstance(m, str) for m in c["match_reasons"])
        assert "similarity" in c
    scores = [c["score"] for c in cands]
    assert scores == sorted(scores, reverse=True)


def test_ac_api_35_different_subjects_get_different_orderings(client, db):
    w = _drill_world(db)
    nod1 = [c["entity_id"] for c in _cands(client, _dup(db, w["subj_nod1"]))]
    lab = [c["entity_id"] for c in _cands(client, _dup(db, w["subj_lab"]))]
    assert nod1[0] == w["c_nod1"]
    assert lab[0] == w["c_lab"]
    assert nod1 != lab
    assert nod1[-1] == w["c_toy"] and lab[-1] == w["c_toy"]


def test_ac_api_35_match_reasons_explain_the_suggestion(client, db):
    w = _drill_world(db)
    by_id = {c["entity_id"]: c for c in _cands(client, _dup(db, w["subj_nod1"]))}
    reasons = by_id[w["c_nod1"]]["match_reasons"]
    assert {"same zone", "similar description", "seen around the same time"} <= set(reasons)
    assert "same module" not in reasons  # same zone already says it
    assert by_id[w["c_toy"]]["match_reasons"] == []
    lab_reasons = {c["entity_id"]: c["match_reasons"] for c in _cands(client, _dup(db, w["subj_lab"]))}
    assert "same module" in lab_reasons[w["c_lab"]]  # LAB1S2 vs LAB1P3


def test_ac_api_35_appearance_dominates_and_is_explained(client, db):
    subject = make_entity(db, label="headset", catalog_id="headset", last_location="LAB1S2", last_seen_at=ts(1))
    make_exemplar(db, subject, vec((0, 1.0)), quality=0.9)
    lookalike = make_entity(db, label="headset", catalog_id="headset", last_location="PMM1D2", last_seen_at=ts(900))
    make_exemplar(db, lookalike, vec((0, 1.0), (1, 0.1)))
    neighbour = make_entity(db, label="headset", catalog_id="headset", last_location="LAB1S2", last_seen_at=ts(2))
    make_exemplar(db, neighbour, vec((3, 1.0)))
    cands = _cands(client, _dup(db, subject))
    assert [c["entity_id"] for c in cands] == [lookalike, neighbour]
    assert "similar appearance" in cands[0]["match_reasons"]
    assert "similar appearance" not in cands[1]["match_reasons"]
    assert cands[0]["similarity"] > 0.9 and cands[1]["similarity"] == pytest.approx(0.0, abs=1e-3)


def test_ac_api_35_similarity_stays_null_without_exemplars(client, db):
    w = _drill_world(db)
    assert all(c["similarity"] is None for c in _cands(client, _dup(db, w["subj_nod1"])))


def test_ac_api_35_stored_candidates_keep_their_order_and_get_scores(client, db):
    w = _drill_world(db)
    stored = [w["c_toy"], w["c_nod1"]]
    r = ok(client.get(f"/review/{_dup(db, w['subj_nod1'], stored)}"))
    assert r["candidates_source"] == "stored"
    assert [c["entity_id"] for c in r["candidates"]] == stored
    assert r["candidates"][1]["score"] > r["candidates"][0]["score"]
    assert "same zone" in r["candidates"][1]["match_reasons"]


def test_ac_api_35_list_scores_match_detail(client, db):
    w = _drill_world(db)
    rid = _dup(db, w["subj_nod1"])
    listed = next(r for r in ok(client.get("/review")) if r["review_id"] == rid)
    assert listed["candidates"] == _cands(client, rid)


# ====================================================================== AC-API-36 label sort
@pytest.fixture()
def c_collation(db):
    """The cluster's CNPG database uses the C collation (byte order: 'B' < 'a'); the local test DB is en_US, which
    would hide the bug. Switch entity.label to C for one test and restore the default afterwards."""
    db.execute('ALTER TABLE entity ALTER COLUMN label TYPE text COLLATE "C"')
    try:
        yield
    finally:
        db.execute('ALTER TABLE entity ALTER COLUMN label TYPE text COLLATE "default"')


def test_ac_api_36_label_sort_is_case_insensitive_with_stable_tiebreak(client, db, c_collation):
    ids = {lab: make_entity(db, label=lab, last_seen_at=ts(i)) for i, lab in
           enumerate(["banana", "Apple", "cherry", "apple", "Banana2"])}
    asc = ok(client.get("/items", params={"sort": "label"}))["items"]
    labels = [i["label"] for i in asc]
    assert [x.lower() for x in labels] == sorted(x.lower() for x in labels)
    assert labels.index("cherry") == 4 and {labels[0], labels[1]} == {"Apple", "apple"}
    assert [i["entity_id"] for i in ok(client.get("/items", params={"sort": "label"}))["items"]] == [
        i["entity_id"] for i in asc
    ]
    desc = [i["label"] for i in ok(client.get("/items", params={"sort": "-label"}))["items"]]
    assert desc[0] == "cherry" and {desc[3], desc[4]} == {"Apple", "apple"}
    assert ids  # all created


def test_ac_api_36_label_sort_pagination_is_consistent(client, db, c_collation):
    for lab in ["b", "B", "a", "A", "c", "C"]:
        make_entity(db, label=lab)
    full = [i["entity_id"] for i in ok(client.get("/items", params={"sort": "label"}))["items"]]
    paged = []
    for off in range(0, 6, 2):
        paged += [i["entity_id"] for i in
                  ok(client.get("/items", params={"sort": "label", "limit": 2, "offset": off}))["items"]]
    assert paged == full


# ====================================================================== AC-API-37 bbox_source
def _frame_review_payload(bbox_source, *, bbox=(0.1, 0.2, 0.3, 0.4), drop_exemplar=False):
    body = payload(events=[], decisions=[])
    s = body["assignments"][0]["sighting"]
    s["bbox"] = list(bbox) if bbox is not None else None
    if bbox_source is not ...:
        s["bbox_source"] = bbox_source
    if drop_exemplar:
        body["assignments"][0]["exemplar"] = None
    body["reviews"][0]["image_uri"] = FRAME
    return body


@pytest.mark.parametrize("source", ["detector", "omni"])
def test_ac_api_37_ingest_stores_bbox_source_and_review_returns_it(client, db, source):
    assert client.post("/ingest/window", json=_frame_review_payload(source)).status_code == 201
    assert db.execute("SELECT bbox_source FROM sighting").fetchone()[0] == source
    r = ok(client.get("/review"))[0]
    assert r["image_bbox"] == pytest.approx([0.1, 0.2, 0.3, 0.4])
    assert r["image_bbox_source"] == source


def test_ac_api_37_old_payloads_and_legacy_sightings_have_null_source(client, db):
    assert client.post("/ingest/window", json=_frame_review_payload(...)).status_code == 201
    assert db.execute("SELECT bbox_source FROM sighting").fetchone()[0] is None
    r = ok(client.get("/review"))[0]
    assert r["image_bbox"] is not None and r["image_bbox_source"] is None


def test_ac_api_37_source_without_a_bbox_is_not_stored(client, db):
    assert client.post("/ingest/window", json=_frame_review_payload("omni", bbox=None)).status_code == 201
    assert db.execute("SELECT bbox, bbox_source FROM sighting").fetchone() == (None, None)
    r = ok(client.get("/review"))[0]
    assert r["image_bbox"] is None and r["image_bbox_source"] is None


def test_ac_api_37_unknown_bbox_source_is_422(client, db):
    assert client.post("/ingest/window", json=_frame_review_payload("guess")).status_code == 422
    assert db.execute("SELECT count(*) FROM sighting").fetchone()[0] == 0


def test_ac_api_37_crop_review_image_has_no_box_or_source(client, db):
    body = _frame_review_payload("detector")
    body["reviews"][0]["image_uri"] = CROP
    assert client.post("/ingest/window", json=body).status_code == 201
    r = ok(client.get("/review"))[0]
    assert r["image_bbox"] is None and r["image_bbox_source"] is None


def test_ac_api_37_item_detail_sightings_carry_bbox_and_source(client, db):
    eid = ok(client.post("/ingest/window", json=_frame_review_payload("detector")), 201)["entities"]["o1"]
    s = ok(client.get(f"/items/{eid}"))["sightings"][0]
    assert s["bbox"] == pytest.approx([0.1, 0.2, 0.3, 0.4]) and s["bbox_source"] == "detector"


def test_ac_api_37_snapshot_bbox_when_the_snapshot_is_a_whole_frame(client, db):
    # omni-only sighting: no exemplar, so the snapshot is the whole frame and the box locates the object in it
    body = _frame_review_payload("omni", drop_exemplar=True)
    eid = ok(client.post("/ingest/window", json=body), 201)["entities"]["o1"]
    item = ok(client.get(f"/items/{eid}"))
    assert item["snapshot_url"] is not None
    assert item["snapshot_bbox"] == pytest.approx([0.1, 0.2, 0.3, 0.4])
    assert item["snapshot_bbox_source"] == "omni"
    listed = ok(client.get("/items"))["items"][0]
    assert listed["snapshot_bbox"] == item["snapshot_bbox"] and listed["snapshot_bbox_source"] == "omni"


def test_ac_api_37_snapshot_bbox_null_when_the_snapshot_is_a_crop(client, db):
    eid = ok(client.post("/ingest/window", json=_frame_review_payload("detector")), 201)["entities"]["o1"]
    item = ok(client.get(f"/items/{eid}"))
    assert item["snapshot_bbox"] is None and item["snapshot_bbox_source"] is None


def test_ac_api_37_snapshot_bbox_follows_the_newest_snapshot_frame(client, db):
    e = make_entity(db, label="laptop", snapshot_uri=FRAME2, last_seen_at=ts(5))
    for minute, uri, box, src in ((1, FRAME, [0.0, 0.0, 0.5, 0.5], "detector"),
                                  (5, FRAME2, [0.5, 0.5, 1.0, 1.0], "omni")):
        db.execute("INSERT INTO sighting (entity_id, seen_at, bbox, bbox_source, confidence, frame_uri) "
                   "VALUES (%s, %s, %s, %s, 0.9, %s)", (e, ts(minute), box, src, uri))
    item = ok(client.get(f"/items/{e}"))
    assert item["snapshot_bbox"] == pytest.approx([0.5, 0.5, 1.0, 1.0]) and item["snapshot_bbox_source"] == "omni"


def test_ac_api_37_snapshot_bbox_hidden_with_the_thumbnail_in_no_capture_zones(client, db):
    subject = make_entity(db, label="bag", catalog_id="m-bag", last_location="NOD2S5", snapshot_uri=FRAME)
    db.execute("INSERT INTO sighting (entity_id, seen_at, bbox, bbox_source, confidence, frame_uri) "
               "VALUES (%s, %s, %s, 'omni', 0.9, %s)", (subject, ts(1), [0.1, 0.1, 0.2, 0.2], FRAME))
    rid = make_review(db, entity_id=subject, kind="unknown_object", image_uri=FRAME2)
    sub = ok(client.get(f"/review/{rid}"))["subject"]
    assert sub["thumbnail_url"] is None and sub["snapshot_bbox"] is None and sub["snapshot_bbox_source"] is None


MIGRATION_004 = CONTRACTS_DB / "004_ui_review_round2.sql"


def test_ac_api_37_migration_004_is_idempotent_on_a_database_with_data(db):
    e = make_entity(db, label="drill", last_location="LAB1S2", last_seen_at=ts(4))
    make_sighting(db, e, seen_at=ts(4), location_id="LAB1S2", frame_uri=FRAME)
    db.execute("ALTER TABLE sighting DROP COLUMN bbox_source CASCADE")
    for _ in range(2):
        db.execute(MIGRATION_004.read_text())
    assert db.execute("SELECT count(*), count(bbox_source) FROM sighting").fetchone() == (1, 0)
    with pytest.raises(psycopg.errors.CheckViolation):
        db.execute("UPDATE sighting SET bbox_source='guess'")
    db.execute("UPDATE sighting SET bbox_source='omni'")


def test_ac_api_37_runner_upgrades_a_003_database_with_data_twice():
    """The real start-up runner on a database built from 001..003 that already holds data (like the cluster)."""
    dbname = f"test_api_mig_{uuid.uuid4().hex[:10]}"
    with psycopg.connect(ADMIN_URL, autocommit=True) as admin:
        admin.execute(f'CREATE DATABASE "{dbname}"')
    url = _url_for(dbname)
    try:
        with psycopg.connect(url, autocommit=True) as conn:
            for name in ("001_init.sql", "002_seed.sql", "003_location_provenance.sql"):
                conn.execute((CONTRACTS_DB / name).read_text())
            t = conn.execute("INSERT INTO entity (label) VALUES ('t') RETURNING entity_id").fetchone()[0]
            s = conn.execute("INSERT INTO entity (label, status, merged_into) VALUES ('s', 'merged', %s) "
                             "RETURNING entity_id", (t,)).fetchone()[0]
            conn.execute("INSERT INTO sighting (entity_id, seen_at, bbox, confidence) VALUES (%s, now(), "
                         "'{0.1,0.1,0.2,0.2}', 0.9)", (t,))
            conn.execute("INSERT INTO audit_log (op, entity_ids, actor) VALUES ('merge', %s::uuid[], 'amy')",
                         ([str(s), str(t)],))
        for _ in range(2):
            applied = apply_migrations(url, CONTRACTS_DB)
            assert "004_ui_review_round2.sql" in applied  # 005 (AC-API-39) now follows it
        with psycopg.connect(url, autocommit=True) as conn:
            assert conn.execute("SELECT count(*), count(bbox_source) FROM sighting").fetchone() == (1, 0)
            assert conn.execute("SELECT count(*) FROM entity").fetchone()[0] == 2
            idx = {r[0] for r in conn.execute("SELECT indexname FROM pg_indexes WHERE schemaname='public'")}
            assert {"entity_merged_into_idx", "audit_log_entity_ids_gin"} <= idx
    finally:
        with psycopg.connect(ADMIN_URL, autocommit=True) as admin:
            admin.execute("SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = %s "
                          "AND pid <> pg_backend_pid()", (dbname,))
            admin.execute(f'DROP DATABASE IF EXISTS "{dbname}"')


# ====================================================================== AC-API-38 event wording support
@pytest.mark.parametrize(
    "event_type, from_loc, to_loc, role",
    [
        ("moved", "LAB1S2", "NOD1D1", "from_to"),
        ("moved", "LAB1S2", None, "from_to"),
        ("used", "USOS", None, "at"),
        ("used", None, "NOD1", "at"),
        ("seen", "LAB1S2", "LAB1S2", "at"),
        ("added", None, "NOD3", "at"),
        ("handed_over", "NOD1", None, "at"),
        ("handed_over", "NOD1", "LAB1", "from_to"),
        ("used", None, None, None),
    ],
)
def test_ac_api_38_location_role(client, db, event_type, from_loc, to_loc, role):
    e = make_entity(db, label="drill")
    make_event(db, e, event_type=event_type, occurred_at=ts(1), from_location=from_loc, to_location=to_loc,
               actor_desc="crew")
    ev = ok(client.get("/events", params={"entity_id": e}))[0]
    assert ev["location_role"] == role
    assert ok(client.get(f"/items/{e}"))["events"][0]["location_role"] == role


@pytest.mark.parametrize(
    "source, actor, kind",
    [("operator", "ui-review-2", "operator"), ("pipeline", "blue shirt", "person"), ("speech", "Chris", "person"),
     ("pipeline", None, None)],
)
def test_ac_api_38_actor_kind(client, db, source, actor, kind):
    e = make_entity(db, label="drill")
    make_event(db, e, event_type="used", occurred_at=ts(1), from_location="NOD1", actor_desc=actor, source=source)
    assert ok(client.get("/events", params={"entity_id": e}))[0]["actor_kind"] == kind
    assert ok(client.get(f"/items/{e}"))["events"][0]["actor_kind"] == kind


def test_ac_api_38_history_event_rows_use_the_same_wording(client, db):
    e = make_entity(db, label="laptop")
    make_event(db, e, event_type="used", occurred_at=ts(1), from_location="USOS", actor_desc="blue shirt")
    make_event(db, e, event_type="moved", occurred_at=ts(2), from_location="USOS", actor_desc="ui-review-2",
               source="operator")
    rows = [h for h in ok(client.get(f"/items/{e}/history")) if h["kind"] == "event"]
    by_type = {h["event_type"]: h for h in rows}
    assert by_type["used"]["summary"] == "used at USOS by blue shirt"
    assert by_type["used"]["location_role"] == "at" and by_type["used"]["actor_kind"] == "person"
    assert by_type["moved"]["summary"] == "moved USOS -> ? by ui-review-2"
    assert by_type["moved"]["location_role"] == "from_to" and by_type["moved"]["actor_kind"] == "operator"


def test_ac_api_33_unmerge_restores_candidate_lists_only_when_unchanged(client, db):
    """Guard: the candidate review's list is restored exactly (source back in its original slot, target kept)."""
    w = _rich_world(db)
    assert _merge(client, w["source"], w["target"]).status_code == 200
    merged = db.execute("SELECT candidate_ids FROM review_item WHERE review_id=%s", (w["cand_review"],)).fetchone()[0]
    assert [str(c) for c in merged] == [w["target"]]
    ok(_unmerge(client, w["source"]))
    restored = db.execute("SELECT candidate_ids FROM review_item WHERE review_id=%s",
                          (w["cand_review"],)).fetchone()[0]
    assert [str(c) for c in restored] == [w["source"], w["target"]]
