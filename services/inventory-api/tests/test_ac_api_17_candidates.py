"""AC-API-17: POST /ingest/candidates (pgvector top-k by max cosine similarity over exemplars)."""
from __future__ import annotations

import pytest

from tests.conftest import make_entity, make_exemplar, ok, parse_dt, ts, vec

Q = vec((0, 1.0))


def _post(client, **body):
    body.setdefault("embedding", Q)
    return client.post("/ingest/candidates", json=body)


def test_ac_api_17_ranked_by_similarity_with_fields(client, db):
    near = make_entity(db, label="near", catalog_id="laptop", last_location="LAB1S2", last_seen_at=ts(4))
    mid = make_entity(db, label="mid", catalog_id="laptop")
    far = make_entity(db, label="far", catalog_id="laptop")
    make_exemplar(db, near, vec((0, 1.0), (1, 0.1)))
    make_exemplar(db, mid, vec((0, 1.0), (1, 1.0)))
    make_exemplar(db, far, vec((1, 1.0)))
    r = _post(client)
    assert r.status_code == 200, r.text
    got = r.json()
    assert [c["entity_id"] for c in got] == [near, mid, far]
    sims = [c["similarity"] for c in got]
    assert sims == sorted(sims, reverse=True)
    assert sims[0] == pytest.approx(0.995, abs=0.01)
    assert sims[1] == pytest.approx(0.7071, abs=0.01)
    assert sims[2] == pytest.approx(0.0, abs=0.01)
    c0 = got[0]
    assert c0["last_location"] == "LAB1S2"
    assert parse_dt(c0["last_seen_at"]) == ts(4)
    assert c0["label"] == "near" and c0["catalog_id"] == "laptop" and "status" in c0


def test_ac_api_17_uses_max_over_exemplars_and_one_row_per_entity(client, db):
    multi = make_entity(db, label="multi-view")
    single = make_entity(db, label="single")
    make_exemplar(db, multi, vec((5, 1.0)))            # unrelated view
    make_exemplar(db, multi, vec((0, 1.0)))            # perfect view
    make_exemplar(db, multi, vec((0, 1.0), (2, 1.0)))  # partial view
    make_exemplar(db, single, vec((0, 1.0), (3, 0.5)))
    got = ok(_post(client))
    assert [c["entity_id"] for c in got] == [multi, single]
    assert got[0]["similarity"] == pytest.approx(1.0, abs=0.01)


def test_ac_api_17_top_k(client, db):
    for i in range(6):
        e = make_entity(db, label=f"e{i}")
        make_exemplar(db, e, vec((0, 1.0), (1, 0.1 * (i + 1))))
    got = ok(_post(client, k=3))
    assert len(got) == 3
    assert [c["label"] for c in got] == ["e0", "e1", "e2"]


def test_ac_api_17_default_k_is_10(client, db):
    for i in range(12):
        e = make_entity(db, label=f"e{i}")
        make_exemplar(db, e, vec((0, 1.0), (i + 1, 0.5)))
    assert len(ok(_post(client))) == 10


def test_ac_api_17_filters_by_catalog_id(client, db):
    laptop = make_entity(db, label="laptop", catalog_id="laptop")
    tablet = make_entity(db, label="tablet", catalog_id="tablet")
    make_exemplar(db, laptop, vec((0, 1.0), (1, 0.5)))
    make_exemplar(db, tablet, vec((0, 1.0)))
    got = ok(_post(client, catalog_id="laptop"))
    assert [c["entity_id"] for c in got] == [laptop]
    assert tablet in [c["entity_id"] for c in ok(_post(client, catalog_id=None))]


def test_ac_api_17_excludes_merged_and_dismissed(client, db):
    live = make_entity(db, label="live", status="confirmed")
    merged = make_entity(db, label="merged", status="merged", merged_into=live)
    dismissed = make_entity(db, label="dismissed", status="dismissed")
    make_exemplar(db, live, vec((0, 1.0), (1, 1.0)))
    make_exemplar(db, merged, vec((0, 1.0)))
    make_exemplar(db, dismissed, vec((0, 1.0)))
    assert [c["entity_id"] for c in ok(_post(client))] == [live]


def test_ac_api_17_entities_without_exemplars_not_returned(client, db):
    make_entity(db, label="no exemplars")
    r = _post(client)
    assert r.status_code == 200
    assert r.json() == []


@pytest.mark.parametrize("embedding", [[0.1] * 767, [0.1] * 769, [], "abc", [None] * 768])
def test_ac_api_17_bad_embedding_is_422(client, db, embedding):
    assert client.post("/ingest/candidates", json={"embedding": embedding}).status_code == 422


def test_ac_api_17_missing_embedding_is_422(client, db):
    assert client.post("/ingest/candidates", json={"k": 3}).status_code == 422


@pytest.mark.parametrize("k", [0, -1])
def test_ac_api_17_non_positive_k_is_422(client, db, k):
    assert _post(client, k=k).status_code == 422
