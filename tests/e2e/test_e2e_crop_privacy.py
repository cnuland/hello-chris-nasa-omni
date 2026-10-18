"""End-to-end: no crop of a person (UI review round 4, finding UI4-01, P1; perception AC-PER-49, inventory-api
AC-API-41) and no station structure in the unknown queue (UI4-05, AC-PER-50).

Its own database, inventory-api and object store (the shared eval scene's counts stay untouched). The real perception
replay runs over 1080x608 frames in which a crew member stands under an unidentified object whose box overlaps them
(the live geometry: a 915x258 crop at the top of the frame), next to a window (station structure) and a laptop clear
of everyone. Afterwards nothing under crops/ shows the person, the unknown's review shows the whole frame and its
sighting is flagged, the laptop keeps its crop, and the window created nothing."""

from __future__ import annotations

import threading
import time
import uuid
from datetime import UTC, datetime, timedelta

import httpx
import psycopg
import pytest
from psycopg import sql

from conftest import ADMIN_URL, BUCKET, CONTRACTS_DB, DB_PREFIX, SharedObjectStore, _drop, _free_port, _url_for

W, H = 1080, 608
T0 = datetime(2026, 10, 18, 4, 36, 45, tzinfo=UTC)


def px(x1, y1, x2, y2):
    return (x1 / W, y1 / H, x2 / W, y2 / H)


ORB = px(0, 0, 915, 258)  # the live crop
PERSON = px(120, 20, 470, 600)
LAPTOP = px(700, 380, 900, 520)
WINDOW = px(40, 30, 300, 200)


@pytest.fixture(scope="module")
def crop_stack():
    try:
        psycopg.connect(ADMIN_URL, connect_timeout=3).close()
    except psycopg.OperationalError as exc:  # pragma: no cover - environment
        pytest.skip(f"local Postgres not reachable at TEST_PG_ADMIN_URL: {exc}")
    import uvicorn
    from inventory_api.config import Settings
    from inventory_api.main import create_app

    name = f"{DB_PREFIX}e2e_crop_{uuid.uuid4().hex[:10]}"
    with psycopg.connect(ADMIN_URL, autocommit=True) as conn:
        conn.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
    url = _url_for(name)
    store = SharedObjectStore()
    server = None
    thread = None
    try:
        with psycopg.connect(url, autocommit=True) as conn:
            for f in sorted(CONTRACTS_DB.glob("*.sql")):
                conn.execute(f.read_text())
        app = create_app(Settings(database_url=url, s3_bucket=BUCKET), object_store=store)
        port = _free_port()
        server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning", lifespan="on"))
        thread = threading.Thread(target=server.run, name="inventory-api-crop", daemon=True)
        thread.start()
        deadline = time.monotonic() + 20
        while not server.started:
            if not thread.is_alive() or time.monotonic() > deadline:
                raise RuntimeError("inventory-api did not start")
            time.sleep(0.02)
        yield f"http://127.0.0.1:{port}", store, url
    finally:
        if server is not None:
            server.should_exit = True
            thread.join(timeout=10)
        _drop(name)


@pytest.fixture(scope="module")
def crop_replay(crop_stack):
    from perception import replay as perception_replay
    from perception.config import PerceptionConfig
    from perception.fakes import FakeDecision, FakeEmbedder, FakeOmni, make_frame
    from perception.inventory_client import HttpInventoryClient
    from perception.types import Detection

    base, store, _ = crop_stack
    painted = [("orb", ORB), ("crew", PERSON), ("laptop", LAPTOP), ("window", WINDOW)]
    items = [(T0 + timedelta(seconds=i), make_frame(painted, height=H, width=W), None) for i in range(6)]
    observation = {
        "scene_summary": "A crew member floats below a round window",
        "location_hint": "Destiny",
        "objects": [
            {"label": "strange orb", "description": "round thing", "confidence": 0.8, "bbox": list(ORB),
             "frame_index": 0},
            {"label": "laptop", "description": "grey laptop", "confidence": 0.9, "bbox": list(LAPTOP),
             "frame_index": 0},
            {"label": "window", "description": "circular window showing Earth below", "confidence": 0.9,
             "bbox": list(WINDOW), "frame_index": 0},
        ],
        "people": [{"ref": "P1", "appearance": "blue shirt, white gloves"}],
        "events": [],
        "transcript": "",
        "audio_events": [],
    }

    class Detector:
        """The detector sees the object box, the person (it queries 'person') and the laptop."""

        def __init__(self):
            self.labels: list[list[str]] = []

        def detect(self, frame, labels):
            self.labels.append(list(labels))
            return [Detection("strange orb", ORB, 0.6), Detection("person", PERSON, 0.9),
                    Detection("laptop", LAPTOP, 0.9), Detection("window", WINDOW, 0.7)]

    detector = Detector()
    client = HttpInventoryClient(base, timeout_s=30)
    try:
        stats = perception_replay.run(
            items, camera_id="cam-handheld", start_time=T0, api=client, omni=FakeOmni([observation] * 4),
            detector=detector, embedder=FakeEmbedder(), decision=FakeDecision(), asr=None, storage=store,
            config=PerceptionConfig(camera_locations={"cam-handheld": "USOS"}, s3_bucket=BUCKET))
    finally:
        client.close()
    return stats, detector


def test_e2e_13_window_with_a_person_and_an_overlapping_box_stores_no_crop(crop_stack, crop_replay):
    base, store, url = crop_stack
    stats, detector = crop_replay
    assert stats.posted >= 1 and stats.errors == 0
    assert stats.person_crops_dropped >= 1
    assert all("person" in labels and "face" in labels for labels in detector.labels)
    with httpx.Client(base_url=base, timeout=10) as api:
        items = api.get("/items", params={"limit": 100}).json()["items"]
        by_label = {}
        for i in items:
            by_label.setdefault(i["label"], []).append(i)
        orb = by_label["strange orb"][0]
        laptop = by_label["laptop"][0]
        # no person crop anywhere: the unknown's snapshot and review image are whole frames
        assert "/crops/" not in (orb["snapshot_url"] or "")
        detail = api.get(f"/items/{orb['entity_id']}").json()
        assert detail["exemplar_count"] == 0
        assert all(s["person_overlap"] for s in detail["sightings"])
        reviews = api.get("/review", params={"kind": "unknown_object"}).json()
        orb_reviews = [r for r in reviews if r["entity_id"] == orb["entity_id"]]
        assert orb_reviews and all("/frames/" in r["image_url"] for r in orb_reviews)
        # the laptop, clear of everyone, keeps its crop
        assert api.get(f"/items/{laptop['entity_id']}").json()["exemplar_count"] >= 1
        # station structure created nothing; the person is a person track, never an object crop
        assert "window" not in by_label
        assert all(r["subject"] is None or r["subject"]["label"] != "window" for r in reviews)
    with psycopg.connect(url) as conn:
        crops = [r[0] for r in conn.execute("SELECT crop_uri FROM entity_exemplar WHERE crop_uri IS NOT NULL")]
        persons = conn.execute("SELECT count(*) FROM entity_exemplar x JOIN entity e USING (entity_id) "
                               "WHERE e.kind = 'person'").fetchone()[0]
    assert persons == 0
    crop_keys = [k for k in store.objects if k.startswith("crops/")]
    assert len(crop_keys) == len(crops) >= 1  # only the laptop's crops were ever uploaded
    assert all(k.split("/", 1)[1] in {c.split("/crops/", 1)[1] for c in crops} for k in crop_keys)
