"""AC-API-2..6 and AC-API-22: GET /items listing, sorting, pagination, filters, location hierarchy, search, status."""
from __future__ import annotations

import pytest

from tests.conftest import iso, make_entity, ok, parse_dt, ts

ITEM_REQUIRED = {"entity_id", "kind", "status", "label", "confidence"}


def ids(resp):
    return [i["entity_id"] for i in ok(resp)["items"]]


# ------------------------------------------------------------------ AC-API-2
def test_ac_api_02_shape_items_and_total(client, db):
    e = make_entity(db, label="Pistol-grip drill", catalog_id="drill-pistol", last_location="LAB1S2",
                    last_seen_at=ts(1), confidence=0.9, status="confirmed")
    r = client.get("/items")
    assert r.status_code == 200
    body = r.json()
    assert set(body) >= {"items", "total"}
    assert body["total"] == 1
    item = body["items"][0]
    assert ITEM_REQUIRED <= set(item)
    assert item["entity_id"] == e
    assert item["catalog_id"] == "drill-pistol"
    assert item["catalog_name"] == "Pistol-grip drill"
    assert item["last_location"] == "LAB1S2"
    assert item["last_location_name"] == "Lab starboard rack 2"
    assert parse_dt(item["last_seen_at"]) == ts(1)
    assert item["confidence"] == pytest.approx(0.9)


def test_ac_api_02_empty_inventory(client, db):
    r = client.get("/items")
    assert r.status_code == 200
    assert r.json() == {"items": [], "total": 0}


def test_ac_api_02_default_sort_last_seen_desc_nulls_last(client, db):
    never = make_entity(db, label="never seen", last_seen_at=None)
    old = make_entity(db, label="old", last_seen_at=ts(1))
    new = make_entity(db, label="new", last_seen_at=ts(30))
    mid = make_entity(db, label="mid", last_seen_at=ts(10))
    r = client.get("/items")
    assert r.status_code == 200
    assert ids(r) == [new, mid, old, never]


def test_ac_api_02_explicit_sort_options(client, db):
    a = make_entity(db, label="alpha", confidence=0.2, last_seen_at=ts(3))
    b = make_entity(db, label="bravo", confidence=0.9, last_seen_at=ts(1))
    c = make_entity(db, label="charlie", confidence=0.5, last_seen_at=ts(2))
    assert ids(client.get("/items", params={"sort": "label"})) == [a, b, c]
    assert ids(client.get("/items", params={"sort": "-label"})) == [c, b, a]
    assert ids(client.get("/items", params={"sort": "confidence"})) == [a, c, b]
    assert ids(client.get("/items", params={"sort": "-confidence"})) == [b, c, a]
    assert ids(client.get("/items", params={"sort": "last_seen_at"})) == [b, c, a]


def test_ac_api_02_ascending_last_seen_sort_still_puts_nulls_last(client, db):
    never = make_entity(db, label="never", last_seen_at=None)
    a = make_entity(db, label="a", last_seen_at=ts(1))
    b = make_entity(db, label="b", last_seen_at=ts(2))
    assert ids(client.get("/items", params={"sort": "last_seen_at"})) == [a, b, never]


def test_ac_api_02_limit_offset_paginate_and_total_counts_all_matches(client, db):
    created = [make_entity(db, label=f"item{i:02d}", last_seen_at=ts(i)) for i in range(7)]
    expected = list(reversed(created))
    p1 = client.get("/items", params={"limit": 3, "offset": 0})
    p2 = client.get("/items", params={"limit": 3, "offset": 3})
    p3 = client.get("/items", params={"limit": 3, "offset": 6})
    assert p1.status_code == p2.status_code == p3.status_code == 200
    assert ids(p1) == expected[0:3]
    assert ids(p2) == expected[3:6]
    assert ids(p3) == expected[6:7]
    assert p1.json()["total"] == p2.json()["total"] == p3.json()["total"] == 7


def test_ac_api_02_offset_past_end_gives_empty_page_with_total(client, db):
    for i in range(3):
        make_entity(db, label=f"x{i}", last_seen_at=ts(i))
    r = client.get("/items", params={"offset": 10})
    assert r.status_code == 200
    assert r.json() == {"items": [], "total": 3}


def test_ac_api_02_default_limit_is_50(client, db):
    for i in range(55):
        make_entity(db, label=f"bulk{i}", last_seen_at=ts(i))
    body = ok(client.get("/items"))
    assert len(body["items"]) == 50
    assert body["total"] == 55


@pytest.mark.parametrize("params", [{"limit": 0}, {"limit": 501}, {"offset": -1}, {"limit": "abc"}])
def test_ac_api_02_invalid_pagination_is_422(client, db, params):
    r = client.get("/items", params=params)
    assert r.status_code == 422


# ------------------------------------------------------------------ AC-API-3
def test_ac_api_03_filter_kind(client, db):
    obj = make_entity(db, label="wrench", kind="object", last_seen_at=ts(1))
    person = make_entity(db, label="crew member, blue shirt", kind="person", last_seen_at=ts(2))
    assert ids(client.get("/items", params={"kind": "person"})) == [person]
    assert ids(client.get("/items", params={"kind": "object"})) == [obj]


def test_ac_api_03_filter_status_repeatable(client, db):
    c = make_entity(db, label="c", status="confirmed", last_seen_at=ts(3))
    p = make_entity(db, label="p", status="provisional", last_seen_at=ts(2))
    u = make_entity(db, label="u", status="unknown", last_seen_at=ts(1))
    assert ids(client.get("/items", params={"status": "unknown"})) == [u]
    r = client.get("/items", params=[("status", "confirmed"), ("status", "unknown")])
    assert ids(r) == [c, u]
    assert r.json()["total"] == 2
    assert p not in ids(r)


def test_ac_api_03_filter_catalog_id(client, db):
    d = make_entity(db, label="drill", catalog_id="drill-pistol", last_seen_at=ts(1))
    make_entity(db, label="wrench", catalog_id="wrench", last_seen_at=ts(2))
    make_entity(db, label="unclassified", catalog_id=None, last_seen_at=ts(3))
    assert ids(client.get("/items", params={"catalog_id": "drill-pistol"})) == [d]


def test_ac_api_03_filter_min_confidence_inclusive(client, db):
    lo = make_entity(db, label="lo", confidence=0.3, last_seen_at=ts(1))
    eq = make_entity(db, label="eq", confidence=0.7, last_seen_at=ts(2))
    hi = make_entity(db, label="hi", confidence=0.95, last_seen_at=ts(3))
    got = ids(client.get("/items", params={"min_confidence": 0.7}))
    assert got == [hi, eq]
    assert lo not in got


def test_ac_api_03_filter_seen_window(client, db):
    make_entity(db, label="before", last_seen_at=ts(0))
    inside = make_entity(db, label="inside", last_seen_at=ts(10))
    make_entity(db, label="after", last_seen_at=ts(20))
    make_entity(db, label="never", last_seen_at=None)
    r = client.get("/items", params={"seen_after": iso(5), "seen_before": iso(15)})
    assert ids(r) == [inside]
    assert r.json()["total"] == 1
    assert len(ids(client.get("/items", params={"seen_after": iso(5)}))) == 2
    assert len(ids(client.get("/items", params={"seen_before": iso(15)}))) == 2


def test_ac_api_03_filter_needs_review(client, db):
    flagged = make_entity(db, label="flagged", needs_review=True, review_reason="unknown", last_seen_at=ts(1))
    ok = make_entity(db, label="ok", needs_review=False, last_seen_at=ts(2))
    assert ids(client.get("/items", params={"needs_review": "true"})) == [flagged]
    assert ids(client.get("/items", params={"needs_review": "false"})) == [ok]


def test_ac_api_03_filters_combine_with_and(client, db):
    match = make_entity(db, label="m", kind="object", status="confirmed", catalog_id="laptop", confidence=0.9,
                        last_location="LAB1S2", last_seen_at=ts(10), needs_review=False)
    make_entity(db, label="wrong kind", kind="person", status="confirmed", catalog_id="laptop", confidence=0.9,
                last_location="LAB1S2", last_seen_at=ts(10))
    make_entity(db, label="low conf", kind="object", status="confirmed", catalog_id="laptop", confidence=0.1,
                last_location="LAB1S2", last_seen_at=ts(10))
    make_entity(db, label="other loc", kind="object", status="confirmed", catalog_id="laptop", confidence=0.9,
                last_location="NOD1D1", last_seen_at=ts(10))
    make_entity(db, label="too old", kind="object", status="confirmed", catalog_id="laptop", confidence=0.9,
                last_location="LAB1S2", last_seen_at=ts(-100))
    make_entity(db, label="other status", kind="object", status="provisional", catalog_id="laptop", confidence=0.9,
                last_location="LAB1S2", last_seen_at=ts(10))
    make_entity(db, label="needs review", kind="object", status="confirmed", catalog_id="laptop", confidence=0.9,
                last_location="LAB1S2", last_seen_at=ts(10), needs_review=True)
    r = client.get("/items", params={
        "kind": "object", "status": "confirmed", "catalog_id": "laptop", "min_confidence": 0.5,
        "location": "LAB1", "seen_after": iso(0), "seen_before": iso(60), "needs_review": "false"})
    assert r.status_code == 200
    assert ids(r) == [match]
    assert r.json()["total"] == 1


# ------------------------------------------------------------------ AC-API-4
def test_ac_api_04_location_matches_self_and_descendants(client, db):
    module = make_entity(db, label="in module", last_location="NOD2", last_seen_at=ts(1))
    zone = make_entity(db, label="in zone", last_location="NOD2D4", last_seen_at=ts(2))
    make_entity(db, label="elsewhere", last_location="NOD1D1", last_seen_at=ts(3))
    make_entity(db, label="nowhere", last_location=None, last_seen_at=ts(4))
    assert ids(client.get("/items", params={"location": "NOD2"})) == [zone, module]


def test_ac_api_04_location_matches_all_levels_of_descendants(client, db):
    zone = make_entity(db, label="zone", last_location="LAB1S2", last_seen_at=ts(1))
    module = make_entity(db, label="module", last_location="NOD1", last_seen_at=ts(2))
    make_entity(db, label="ros", last_location="ZVEZDA", last_seen_at=ts(3))
    got = ids(client.get("/items", params={"location": "USOS"}))
    assert sorted(got) == sorted([zone, module])


def test_ac_api_04_location_leaf_does_not_match_parent_or_siblings(client, db):
    leaf = make_entity(db, label="leaf", last_location="NOD2D4", last_seen_at=ts(1))
    make_entity(db, label="parent", last_location="NOD2", last_seen_at=ts(2))
    make_entity(db, label="sibling", last_location="NOD2O4", last_seen_at=ts(3))
    assert ids(client.get("/items", params={"location": "NOD2D4"})) == [leaf]


def test_ac_api_04_location_uses_hierarchy_not_string_prefix(client, db):
    # A location whose id starts with 'NOD2' but is NOT a child of NOD2 must not match location=NOD2.
    db.execute("INSERT INTO location (location_id, parent_id, kind, display_name) VALUES "
               "('NOD2X', 'USOS', 'module', 'Prefix trap')")
    trap = make_entity(db, label="trap", last_location="NOD2X", last_seen_at=ts(5))
    real = make_entity(db, label="real", last_location="NOD2D4", last_seen_at=ts(1))
    got = ids(client.get("/items", params={"location": "NOD2"}))
    assert got == [real]
    assert trap not in got


def test_ac_api_04_location_with_custom_deeper_child(client, db):
    db.execute("INSERT INTO location (location_id, parent_id, kind, display_name) VALUES "
               "('BAG-17', 'NOD2D4', 'container', 'CTB in Node 2 deck 4')")
    deep = make_entity(db, label="in bag", last_location="BAG-17", last_seen_at=ts(1))
    assert ids(client.get("/items", params={"location": "NOD2"})) == [deep]


def test_ac_api_04_unknown_location_yields_no_items_not_error(client, db):
    make_entity(db, label="x", last_location="NOD2", last_seen_at=ts(1))
    r = client.get("/items", params={"location": "MARS1"})
    assert r.status_code == 200
    assert r.json() == {"items": [], "total": 0}


# ------------------------------------------------------------------ AC-API-5
def test_ac_api_05_q_matches_label_case_insensitive(client, db):
    d = make_entity(db, label="Pistol-Grip DRILL", last_seen_at=ts(1))
    make_entity(db, label="wrench", last_seen_at=ts(2))
    assert ids(client.get("/items", params={"q": "drill"})) == [d]
    assert ids(client.get("/items", params={"q": "pistol-grip"})) == [d]


def test_ac_api_05_q_matches_description(client, db):
    e = make_entity(db, label="bag", description="White cargo transfer bag with RED velcro strap", last_seen_at=ts(1))
    make_entity(db, label="other", description="nothing", last_seen_at=ts(2))
    assert ids(client.get("/items", params={"q": "red velcro"})) == [e]


def test_ac_api_05_q_matches_catalog_display_name(client, db):
    e = make_entity(db, label="the meter", catalog_id="multimeter", last_seen_at=ts(1))
    make_entity(db, label="other", last_seen_at=ts(2))
    assert ids(client.get("/items", params={"q": "fluke multimeter"})) == [e]


def test_ac_api_05_q_matches_catalog_synonyms(client, db):
    e = make_entity(db, label="handheld tool", catalog_id="drill-pistol", last_seen_at=ts(1))
    make_entity(db, label="other", catalog_id="wrench", last_seen_at=ts(2))
    assert ids(client.get("/items", params={"q": "PGT"})) == [e]
    assert ids(client.get("/items", params={"q": "power drill"})) == [e]


def test_ac_api_05_q_no_match_is_empty(client, db):
    make_entity(db, label="wrench", last_seen_at=ts(1))
    r = client.get("/items", params={"q": "zzzqqqxxx"})
    assert r.status_code == 200
    assert r.json() == {"items": [], "total": 0}


def test_ac_api_05_q_ranks_by_relevance_without_explicit_sort(client, db):
    weak = make_entity(db, label="tool kit", description="kit includes a drill bit", last_seen_at=ts(50))
    strong = make_entity(db, label="drill", last_seen_at=ts(1))
    r = client.get("/items", params={"q": "drill"})
    assert r.status_code == 200
    assert ids(r) == [strong, weak]


def test_ac_api_05_explicit_sort_overrides_relevance(client, db):
    weak = make_entity(db, label="tool kit", description="kit includes a drill bit", last_seen_at=ts(50))
    strong = make_entity(db, label="drill", last_seen_at=ts(1))
    r = client.get("/items", params={"q": "drill", "sort": "-last_seen_at"})
    assert ids(r) == [weak, strong]


def test_ac_api_05_q_is_not_sql_injectable(client, db):
    make_entity(db, label="wrench", last_seen_at=ts(1))
    r = client.get("/items", params={"q": "'; DROP TABLE entity; --"})
    assert r.status_code == 200
    assert db.execute("SELECT count(*) FROM entity").fetchone()[0] == 1


def test_ac_api_05_q_with_like_wildcards_is_literal(client, db):
    make_entity(db, label="wrench", last_seen_at=ts(1))
    r = client.get("/items", params={"q": "%"})
    assert r.status_code == 200
    assert r.json()["total"] == 0


# ------------------------------------------------------------------ AC-API-6
def _merged_dismissed(db):
    target = make_entity(db, label="target", status="confirmed", last_seen_at=ts(3))
    merged = make_entity(db, label="dup", status="merged", merged_into=target, last_seen_at=ts(2))
    dismissed = make_entity(db, label="glare", status="dismissed", last_seen_at=ts(1))
    return target, merged, dismissed


def test_ac_api_06_merged_and_dismissed_hidden_by_default(client, db):
    target, _merged, _dismissed = _merged_dismissed(db)
    r = client.get("/items")
    assert ids(r) == [target]
    assert r.json()["total"] == 1


def test_ac_api_06_merged_and_dismissed_hidden_from_search_and_filters(client, db):
    _target, _merged, _dismissed = _merged_dismissed(db)
    assert ids(client.get("/items", params={"q": "dup"})) == []
    assert ids(client.get("/items", params={"q": "glare"})) == []


def test_ac_api_06_explicit_status_includes_merged(client, db):
    target, merged, dismissed = _merged_dismissed(db)
    assert ids(client.get("/items", params={"status": "merged"})) == [merged]
    assert ids(client.get("/items", params={"status": "dismissed"})) == [dismissed]
    got = ids(client.get("/items", params=[("status", "confirmed"), ("status", "merged"), ("status", "dismissed")]))
    assert got == [target, merged, dismissed]


# ------------------------------------------------------------------ AC-API-22
@pytest.mark.parametrize("params", [
    {"status": "bogus"},
    [("status", "confirmed"), ("status", "nope")],
    {"kind": "robot"},
    {"sort": "entity_id; drop"},
    {"min_confidence": 1.5},
    {"min_confidence": -0.1},
    {"needs_review": "maybe"},
    {"seen_after": "not-a-date"},
])
def test_ac_api_22_invalid_enum_or_value_is_422_never_500(client, db, params):
    make_entity(db, label="x", last_seen_at=ts(1))
    r = client.get("/items", params=params)
    assert r.status_code == 422, r.text
