"""AC-API-14: GET/POST /catalog, GET /locations, GET /events filters."""
from __future__ import annotations

import uuid

import pytest

from tests.conftest import iso, make_entity, make_event, ok, parse_dt, table_counts, ts

NEW_ITEM = {"catalog_id": "torque-driver", "class_name": "torque driver", "display_name": "Torque driver",
            "synonyms": ["torque screwdriver"], "category": "object", "is_fungible": False, "expected_count": 2,
            "home_location": "LAB1"}


def test_ac_api_14_get_catalog_lists_seed(client, db):
    r = client.get("/catalog")
    assert r.status_code == 200
    cat = {c["catalog_id"]: c for c in r.json()}
    assert len(cat) == 24
    drill = cat["drill-pistol"]
    assert drill["class_name"] == "drill"
    assert drill["display_name"] == "Pistol-grip drill"
    assert "PGT" in drill["synonyms"]
    assert drill["category"] == "object"
    assert drill["expected_count"] == 2
    assert drill["home_location"] == "LAB1"
    assert cat["food-pack"]["is_fungible"] is True


def test_ac_api_14_post_catalog_creates_201(client, db):
    r = client.post("/catalog", json=NEW_ITEM)
    assert r.status_code == 201, r.text
    row = db.execute("SELECT class_name, display_name, synonyms, expected_count, home_location FROM catalog_item "
                     "WHERE catalog_id='torque-driver'").fetchone()
    assert row == ("torque driver", "Torque driver", ["torque screwdriver"], 2, "LAB1")
    assert "torque-driver" in [c["catalog_id"] for c in ok(client.get("/catalog"))]


def test_ac_api_14_post_catalog_minimal_fields_defaults(client, db):
    r = client.post("/catalog", json={"catalog_id": "spoon", "class_name": "spoon", "display_name": "Spoon"})
    assert r.status_code == 201
    row = db.execute("SELECT synonyms, category, is_fungible FROM catalog_item WHERE catalog_id='spoon'").fetchone()
    assert row == ([], "object", False)


def test_ac_api_14_post_catalog_duplicate_is_409(client, db):
    before = table_counts(db)
    r = client.post("/catalog", json=dict(NEW_ITEM, catalog_id="drill-pistol"))
    assert r.status_code == 409
    assert table_counts(db) == before
    assert db.execute("SELECT display_name FROM catalog_item WHERE catalog_id='drill-pistol'").fetchone()[0] == \
        "Pistol-grip drill"


@pytest.mark.parametrize("bad_id", ["Drill", "a", "-drill", "drill pistol", "drill_pistol", "x" * 65, "",
                                    "../etc", "drill;drop"])
def test_ac_api_14_post_catalog_bad_id_pattern_is_422(client, db, bad_id):
    before = table_counts(db)
    r = client.post("/catalog", json=dict(NEW_ITEM, catalog_id=bad_id))
    assert r.status_code == 422
    assert table_counts(db) == before


@pytest.mark.parametrize("ok_id", ["ab", "0-ring", "x" * 64])
def test_ac_api_14_post_catalog_pattern_boundaries_accepted(client, db, ok_id):
    r = client.post("/catalog", json=dict(NEW_ITEM, catalog_id=ok_id))
    assert r.status_code == 201


@pytest.mark.parametrize("patch", [{"class_name": None}, {"display_name": None}, {"category": "vehicle"},
                                   {"home_location": "MARS1"}])
def test_ac_api_14_post_catalog_invalid_fields_are_422(client, db, patch):
    body = {k: v for k, v in dict(NEW_ITEM, **patch).items() if v is not None}
    r = client.post("/catalog", json=body)
    assert r.status_code == 422


def test_ac_api_14_get_locations_hierarchy(client, db):
    r = client.get("/locations")
    assert r.status_code == 200
    locs = {loc["location_id"]: loc for loc in r.json()}
    assert len(locs) == 30
    assert locs["NOD2D4"]["parent_id"] == "NOD2"
    assert locs["NOD2D4"]["kind"] == "zone"
    assert locs["NOD2D4"]["display_name"] == "Node 2 deck bay 4"
    assert locs["USOS"]["parent_id"] is None


# ------------------------------------------------------------------ /events
@pytest.fixture()
def events(db):
    a = make_entity(db, label="a")
    b = make_entity(db, label="b")
    ids = {
        "a_moved_1": make_event(db, a, event_type="moved", occurred_at=ts(1)),
        "a_used_5": make_event(db, a, event_type="used", occurred_at=ts(5)),
        "b_moved_9": make_event(db, b, event_type="moved", occurred_at=ts(9)),
    }
    return a, b, ids


def _ev_ids(r):
    assert r.status_code == 200, r.text
    return {e["event_id"] for e in r.json()}


def test_ac_api_14_events_all(client, db, events):
    _a, _b, ids = events
    assert _ev_ids(client.get("/events")) == set(ids.values())


def test_ac_api_14_events_sorted_newest_first(client, db, events):
    times = [parse_dt(e["occurred_at"]) for e in ok(client.get("/events"))]
    assert times == sorted(times, reverse=True)


def test_ac_api_14_events_filter_entity(client, db, events):
    _a, b, ids = events
    assert _ev_ids(client.get("/events", params={"entity_id": b})) == {ids["b_moved_9"]}


def test_ac_api_14_events_filter_type(client, db, events):
    _a, _b, ids = events
    assert _ev_ids(client.get("/events", params={"type": "moved"})) == {ids["a_moved_1"], ids["b_moved_9"]}


def test_ac_api_14_events_filter_time_window(client, db, events):
    _a, _b, ids = events
    assert _ev_ids(client.get("/events", params={"after": iso(2), "before": iso(8)})) == {ids["a_used_5"]}


def test_ac_api_14_events_filters_combine(client, db, events):
    a, _b, ids = events
    assert _ev_ids(client.get("/events", params={"entity_id": a, "type": "moved"})) == {ids["a_moved_1"]}


def test_ac_api_14_events_limit(client, db, events):
    assert len(ok(client.get("/events", params={"limit": 2}))) == 2


def test_ac_api_14_events_bad_params_are_422(client, db):
    assert client.get("/events", params={"entity_id": "nope"}).status_code == 422
    assert client.get("/events", params={"after": "yesterday"}).status_code == 422


def test_ac_api_14_events_unknown_entity_empty(client, db, events):
    assert _ev_ids(client.get("/events", params={"entity_id": str(uuid.uuid4())})) == set()
