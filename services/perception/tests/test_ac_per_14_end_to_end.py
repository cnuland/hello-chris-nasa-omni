"""AC-PER-14 End to end: fake omni + fake detector + fake decision + test DB via the real inventory-api app. A scripted
20-window scenario with 3 objects (one seen in 10 windows, one moved, one unknown) yields exactly 3 non-merged
entities, 1 `moved` event and 1 `unknown_object` review.

Also covers HttpInventoryClient (the InventoryClient used by replay) against the same in-process inventory-api."""
from __future__ import annotations

import uuid
from datetime import timedelta

import pytest
from support import BUCKET, CAMERA_LOCATIONS, T0, iso, obj, observation

import perception.replay
from perception import NEW
from perception.config import PerceptionConfig
from perception.fakes import FakeASR, FakeDecision, FakeDetector, FakeEmbedder, FakeOmni, MemoryStorage, make_frame
from perception.inventory_client import HttpInventoryClient, InventoryError
from perception.types import DecisionResult

pytestmark = pytest.mark.db

LAPTOP_BOX = (0.05, 0.10, 0.30, 0.45)
DRILL_BOX = (0.40, 0.10, 0.60, 0.40)
ORB_BOX = (0.70, 0.55, 0.90, 0.85)
STEP_S = 10.0


@pytest.fixture()
def api(api_app_client):
    return HttpInventoryClient("http://testserver", http_client=api_app_client)


def affirm_top_candidate(question, options, state):
    """Fake decision model: for same_or_new it confirms the first candidate; anything else -> failure."""
    if question != "same_or_new":
        return TimeoutError("not scripted")
    keys = list(options.keys()) if isinstance(options, dict) else list(options)
    first = next(k for k in keys if k != NEW)
    return DecisionResult(choice=first, probabilities={first: 0.9, NEW: 0.1}, model="fake-decision", latency_ms=3)


def scenario():
    """20 windows, one frame each, 10 s apart (camera cam-lab1-fwd, LAB1).

    - laptop-A: windows 0-9 (seen in 10 windows)
    - drill-1: windows 0-4 in LAB1; windows 10-14 in NOD2 (location_hint), with an omni `moved` event in window 10
    - orb-1 (not in the catalog): windows 15-19
    """
    source, observations = [], []
    for i in range(20):
        painted, objects, events, hint = [], [], [], None
        if i < 10:
            painted.append(("laptop-A", LAPTOP_BOX))
            objects.append(obj("laptop", LAPTOP_BOX, description="grey SSC laptop"))
        if i < 5 or 10 <= i < 15:
            painted.append(("drill-1", DRILL_BOX))
            objects.append(obj("drill", DRILL_BOX, description="pistol-grip drill"))
        if 10 <= i < 15:
            hint = "NOD2"
        if i == 10:
            events.append({"type": "moved", "object_label": "drill", "actor_ref": None, "to_location_hint": "NOD2",
                           "frame_index": 0, "evidence": "drill now in node 2", "confidence": 0.8})
        if i >= 15:
            painted.append(("orb-1", ORB_BOX))
            objects.append(obj("strange cylindrical device", ORB_BOX, description="cylinder with a red cap"))
        source.append((T0 + timedelta(seconds=i * STEP_S), make_frame(painted), None))
        observations.append(observation(objects, events=events, location_hint=hint,
                                        scene_summary=f"window {i}"))
    return source, observations


def test_ac_per_14_scripted_scenario_end_to_end(api, db):
    source, observations = scenario()
    config = PerceptionConfig(camera_locations=dict(CAMERA_LOCATIONS), s3_bucket=BUCKET, window_frames=1,
                              stride_s=1.0)
    stats = perception.replay.run(
        source, camera_id="cam-lab1-fwd", start_time=T0, api=api, omni=FakeOmni(observations),
        detector=FakeDetector({"laptop-A": "laptop", "drill-1": "drill", "orb-1": "object"}),
        embedder=FakeEmbedder(), decision=FakeDecision(affirm_top_candidate), asr=FakeASR(),
        storage=MemoryStorage(BUCKET), config=config)

    assert stats.windows == 20 and stats.posted == 20 and stats.invalid == 0 and stats.errors == 0
    assert db.execute("SELECT count(*) FROM observation_window").fetchone()[0] == 20

    live = db.execute("SELECT entity_id, catalog_id, status, last_location FROM entity WHERE status <> 'merged'"
                      ).fetchall()
    assert len(live) == 3, live
    by_cat = {r[1]: r for r in live}
    assert set(by_cat) == {"laptop", "drill-pistol", None}
    assert by_cat[None][2] == "unknown"
    assert by_cat["drill-pistol"][3] == "NOD2"
    assert by_cat["laptop"][3] == "LAB1"

    moved = db.execute("SELECT entity_id, to_location FROM event WHERE event_type = 'moved'").fetchall()
    assert len(moved) == 1
    assert moved[0][0] == by_cat["drill-pistol"][0] and moved[0][1] == "NOD2"

    unknown_reviews = db.execute("SELECT entity_id, image_uri, description FROM review_item "
                                 "WHERE kind = 'unknown_object'").fetchall()
    assert len(unknown_reviews) == 1
    assert unknown_reviews[0][0] == by_cat[None][0]
    assert unknown_reviews[0][1].startswith(f"s3://{BUCKET}/crops/")
    assert "red cap" in unknown_reviews[0][2]

    sightings = dict(db.execute("SELECT entity_id, count(*) FROM sighting GROUP BY entity_id").fetchall())
    assert sightings[by_cat["laptop"][0]] == 10
    assert sightings[by_cat["drill-pistol"][0]] == 10
    assert sightings[by_cat[None][0]] == 5


# ------------------------------------------------------------------------------------------------ HttpInventoryClient
def _vec(i: int) -> list[float]:
    v = [0.0] * 768
    v[i] = 1.0
    return v


def _payload(ref="r1", catalog_id="laptop", embedding=None, camera_id="cam-lab1-fwd"):
    return {
        "window": {"camera_id": camera_id, "t_start": iso(T0), "t_end": iso(T0 + timedelta(seconds=5)),
                   "model": "fake-omni", "scene_summary": "test"},
        "assignments": [{
            "entity_id": None, "ref": ref,
            "new_entity": {"catalog_id": catalog_id, "kind": "object", "status": "provisional", "label": "laptop"},
            "sighting": {"location_id": "LAB1", "seen_at": iso(T0), "confidence": 0.9},
            "exemplar": {"embedding": embedding or _vec(0), "quality": 0.8},
        }],
    }


def test_ac_per_14_client_reads_catalog_and_locations(api):
    catalog = api.get_catalog()
    assert len(catalog) == 24 and {"drill-pistol", "laptop"} <= {c["catalog_id"] for c in catalog}
    locations = api.get_locations()
    assert len(locations) == 30
    assert any(loc["location_id"] == "NOD2S5" for loc in locations)


def test_ac_per_14_client_post_window_and_candidates(api):
    assert api.candidates(_vec(0)) == []
    resp = api.post_window(_payload())
    eid = resp["entities"]["r1"]
    uuid.UUID(str(eid))
    cands = api.candidates(_vec(0), catalog_id="laptop", k=5)
    assert [str(c["entity_id"]) for c in cands] == [str(eid)]
    assert cands[0]["similarity"] == pytest.approx(1.0, abs=1e-3)
    assert api.candidates(_vec(0), catalog_id="drill-pistol") == []


def test_ac_per_14_client_count_list_and_patch(api):
    assert api.count_live_entities("laptop") == 0
    eid = str(api.post_window(_payload())["entities"]["r1"])
    assert api.count_live_entities("laptop") == 1
    items = api.list_items(catalog_id="laptop")
    assert [str(i["entity_id"]) for i in items] == [eid]
    patched = api.patch_item(eid, {"needs_review": True})
    assert patched["needs_review"] is True


def test_ac_per_14_client_raises_inventory_error_on_422(api, db):
    with pytest.raises(InventoryError) as exc:
        api.post_window(_payload(camera_id="cam-does-not-exist"))
    assert exc.value.status_code == 422
    assert db.execute("SELECT count(*) FROM observation_window").fetchone()[0] == 0
