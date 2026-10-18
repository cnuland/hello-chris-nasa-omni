"""End-to-end: structure memory (UI review round 6, finding UI6-05; inventory-api AC-API-46, perception AC-PER-64).

The live loop through the real inventory-api: perception raises the unknown "insulated hose", an operator dismisses it
as Not inventory, GET /structure-memory lists it, and the same duct seen again as "white padded tube" is suppressed
(no entity, no sighting, no review). Runs in Columbus at a time far from the eval scene so no other test sees it."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import numpy as np
import psycopg

from conftest import BUCKET

T = datetime(2026, 10, 18, 7, 59, 1, tzinfo=UTC)


def _reconciler(inventory_api):
    from perception.config import PerceptionConfig
    from perception.fakes import FakeDecision, FakeEmbedder, MemoryStorage
    from perception.inventory_client import HttpInventoryClient
    from perception.reconciler import Reconciler

    client = HttpInventoryClient(inventory_api, timeout_s=30)
    cfg = PerceptionConfig(camera_locations={"cam-handheld": "USOS"}, s3_bucket=BUCKET, revisit_location_sim=0.0)
    return Reconciler(api=client, embedder=FakeEmbedder(), decision=FakeDecision(), storage=MemoryStorage(BUCKET),
                      config=cfg, camera_id="cam-handheld"), client


def _window(rec, t, label, description):
    from perception.types import Keyframe, OmniResult, Window

    frame = Keyframe(timestamp=t, image=np.zeros((48, 64, 3), dtype=np.uint8), pts=0.0, index=0)
    obs = {"scene_summary": "a module interior", "location_hint": "Columbus", "transcript": "", "audio_events": [],
           "people": [], "events": [],
           "objects": [{"label": label, "description": description, "confidence": 0.8, "bbox": [0.1, 0.1, 0.5, 0.4],
                        "frame_index": 0}]}
    out = rec.process(Window(t_start=t, t_end=t, frames=[frame], audio=None, index=0),
                      OmniResult(status="ok", observation=obs, model="e2e-omni", raw="{}"), [[]])
    assert out.status == "posted", out.error
    return out


def test_e2e_13_not_inventory_dismissal_suppresses_the_same_duct_later(inventory_api, api, database_url):
    rec, client = _reconciler(inventory_api)
    try:
        first = _window(rec, T, "insulated hose", "thick white insulated hose curving through the foreground")
        [review] = [r for r in api.get("/review", params={"kind": "unknown_object", "limit": 200}).json()
                    if r.get("model_guess") == "insulated hose"]
        resolved = api.post(f"/review/{review['review_id']}/resolve",
                            json={"action": "dismiss", "note": "not_inventory", "operator": "e2e"})
        assert resolved.status_code == 200, resolved.text
        memory = [m for m in api.get("/structure-memory").json() if m["label_key"] == "insulatedhose"]
        assert len(memory) == 1 and memory[0]["module_id"] == "COL1"
        rec._memories_at = None  # the reconciler's cache would refresh within a minute anyway
        later = _window(rec, T + timedelta(minutes=11), "white padded tube",
                        "a thick white insulated tube running through the module")
    finally:
        client.close()
    assert first.payload["assignments"] and later.payload["assignments"] == []
    assert later.payload["window"]["structure_memory_suppressed"] == 1
    with psycopg.connect(database_url) as conn:
        assert conn.execute("SELECT count(*) FROM entity WHERE label = 'white padded tube'").fetchone()[0] == 0
