"""AC-API-15 /search and AC-API-16 /stats.

Contract decisions made by these tests (OpenAPI leaves the bodies open):
- /search returns a JSON list of hits (or {"hits": [...]}); hit.type is "entity" or "window"; id is entity_id or
  window_id; snippet is a non-empty string; time is ISO date-time (entity last_seen_at / window t_start).
- /stats returns {"by_status": {...}, "by_kind": {...}, "by_module": {...}, "open_reviews_by_kind": {...}}.
"""
from __future__ import annotations

from tests.conftest import make_entity, make_review, make_window, parse_dt, ts


def _hits(r):
    assert r.status_code == 200, r.text
    body = r.json()
    return body if isinstance(body, list) else body["hits"]


def test_ac_api_15_search_entity_hits(client, db):
    e = make_entity(db, label="Nikon camera", description="Z9 with long lens", last_seen_at=ts(3))
    make_entity(db, label="wrench", last_seen_at=ts(4))
    hits = _hits(client.get("/search", params={"q": "camera"}))
    ent = [h for h in hits if h["type"] == "entity"]
    assert [h["id"] for h in ent] == [e]
    h = ent[0]
    assert isinstance(h["snippet"], str) and h["snippet"]
    assert parse_dt(h["time"]) == ts(3)


def test_ac_api_15_search_window_hits_scene_summary_and_transcript(client, db):
    w1 = make_window(db, t_start=ts(1), scene_summary="Crew member stows the drill in a cargo bag")
    w2 = make_window(db, t_start=ts(2), transcript="Houston, I put the drill back in Node two")
    make_window(db, t_start=ts(3), scene_summary="Crew eating lunch")
    hits = _hits(client.get("/search", params={"q": "drill"}))
    win = {h["id"]: h for h in hits if h["type"] == "window"}
    assert set(win) == {w1, w2}
    assert "drill" in win[w1]["snippet"].lower()
    assert "drill" in win[w2]["snippet"].lower()
    assert parse_dt(win[w1]["time"]) == ts(1)


def test_ac_api_15_search_full_text_stemming(client, db):
    w = make_window(db, t_start=ts(1), scene_summary="Astronaut moving the laptops to the lab")
    hits = _hits(client.get("/search", params={"q": "laptop moved"}))
    assert w in [h["id"] for h in hits if h["type"] == "window"]


def test_ac_api_15_search_mixes_types_each_with_required_fields(client, db):
    make_entity(db, label="drill", last_seen_at=ts(1))
    make_window(db, t_start=ts(2), scene_summary="the drill floats by")
    hits = _hits(client.get("/search", params={"q": "drill"}))
    assert {h["type"] for h in hits} == {"entity", "window"}
    for h in hits:
        assert {"type", "id", "snippet", "time"} <= set(h)


def test_ac_api_15_search_excludes_merged_and_dismissed_entities(client, db):
    t = make_entity(db, label="drill", status="confirmed")
    make_entity(db, label="drill dup", status="merged", merged_into=t)
    make_entity(db, label="drill glare", status="dismissed")
    ids = [h["id"] for h in _hits(client.get("/search", params={"q": "drill"})) if h["type"] == "entity"]
    assert ids == [t]


def test_ac_api_15_search_limit(client, db):
    for i in range(5):
        make_window(db, t_start=ts(i), scene_summary=f"drill sighting number {i}")
    assert len(_hits(client.get("/search", params={"q": "drill", "limit": 3}))) == 3


def test_ac_api_15_search_requires_q(client, db):
    assert client.get("/search").status_code == 422


def test_ac_api_15_search_no_hits(client, db):
    make_window(db, scene_summary="quiet module")
    assert _hits(client.get("/search", params={"q": "zzzqqq"})) == []


def test_ac_api_15_search_punctuation_does_not_500(client, db):
    make_window(db, scene_summary="drill")
    r = client.get("/search", params={"q": "drill & | ! ( ' :*"})
    assert r.status_code == 200


# ------------------------------------------------------------------ AC-API-16
def test_ac_api_16_stats_counts(client, db):
    make_entity(db, label="a", status="confirmed", kind="object", last_location="NOD2D4")
    make_entity(db, label="b", status="confirmed", kind="object", last_location="NOD2")
    make_entity(db, label="c", status="unknown", kind="object", last_location="LAB1S2")
    make_entity(db, label="p", status="provisional", kind="person", last_location="LAB1P3")
    e = make_entity(db, label="u", status="unknown", kind="object", last_location=None)
    make_review(db, entity_id=e, kind="unknown_object")
    make_review(db, entity_id=e, kind="unknown_object")
    make_review(db, entity_id=e, kind="possible_duplicate")
    make_review(db, entity_id=e, kind="low_confidence", status="resolved")
    r = client.get("/stats")
    assert r.status_code == 200
    s = r.json()
    assert s["by_status"].get("confirmed") == 2
    assert s["by_status"].get("unknown") == 2
    assert s["by_status"].get("provisional") == 1
    assert s["by_kind"].get("object") == 4
    assert s["by_kind"].get("person") == 1
    # zones roll up to their module
    assert s["by_module"].get("NOD2") == 2
    assert s["by_module"].get("LAB1") == 2
    assert s["open_reviews_by_kind"].get("unknown_object") == 2
    assert s["open_reviews_by_kind"].get("possible_duplicate") == 1
    assert s["open_reviews_by_kind"].get("low_confidence", 0) == 0


def test_ac_api_16_stats_empty(client, db):
    r = client.get("/stats")
    assert r.status_code == 200
    s = r.json()
    for key in ("by_status", "by_kind", "by_module", "open_reviews_by_kind"):
        assert key in s
        assert sum(s[key].values()) == 0
