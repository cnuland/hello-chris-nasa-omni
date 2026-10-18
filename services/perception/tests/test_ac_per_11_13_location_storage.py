"""AC-PER-11 location: camera default, overridden by location_hint when it maps to a known location id; no_capture
windows are summarised only (no frames/crops stored, no reviews).
AC-PER-13 storage: frames, crops and raw model response under frames/<camera>/<date>/<ts>.jpg, crops/..., raw/...;
uploads skipped in no-capture."""
from __future__ import annotations

import json
import re
from datetime import UTC, datetime

import boto3
import pytest
from moto import mock_aws
from support import BUCKET, LOCATIONS, detections_for, obj, observation, scene_window, uri_key
from test_ac_per_08_reconciler import BOX_A, BOX_B, process

from perception.locations import LocationResolver
from perception.storage import S3Storage, crop_key, frame_key, raw_key
from perception.types import OmniResult


@pytest.fixture()
def resolver():
    return LocationResolver(LOCATIONS)


# ------------------------------------------------------------------------------------------------ resolver
@pytest.mark.parametrize("hint,expected", [
    ("NOD2", "NOD2"), ("nod2", "NOD2"), (" LAB1S2 ", "LAB1S2"), ("Node 2", "NOD2"), ("node two", "NOD2"),
    ("Harmony", "NOD2"), ("Node 2 Harmony", "NOD2"), ("Destiny", "LAB1"), ("US Lab", "LAB1"), ("Columbus", "COL1"),
    ("Kibo JEM Pressurized Module", "JPM1"), ("Lab starboard rack 2", "LAB1S2"), ("Node 1", "NOD1"),
    ("Zvezda", "ZVEZDA"), ("cupola", "CUP1"),
])
def test_ac_per_11_resolve_hint_to_known_location(resolver, hint, expected):
    assert resolver.resolve(hint) == expected


@pytest.mark.parametrize("hint", [None, "", "   ", "Narnia", "node 9", "the place over there"])
def test_ac_per_11_unknown_hint_resolves_to_none(resolver, hint):
    assert resolver.resolve(hint) is None


def test_ac_per_11_no_capture_flags_and_inheritance(resolver):
    assert resolver.is_no_capture("NOD2S5") is True
    assert resolver.is_no_capture("NOD3F4") is True
    assert resolver.is_no_capture("NOD2") is False
    assert resolver.is_no_capture("LAB1S2") is False
    assert resolver.is_no_capture(None) is False
    assert resolver.is_no_capture("UNKNOWN-LOC") is False
    custom = LocationResolver(LOCATIONS + [{"location_id": "NOD2S5-BAG", "parent_id": "NOD2S5", "kind": "container",
                                             "display_name": "bag in crew quarters", "no_capture": False}])
    assert custom.is_no_capture("NOD2S5-BAG") is True


def test_ac_per_11_module_of(resolver):
    assert resolver.module_of("NOD2D4") == "NOD2"
    assert resolver.module_of("NOD2") == "NOD2"
    assert resolver.module_of("LAB1S2") == "LAB1"
    assert resolver.module_of("USOS") is None


def test_ac_per_11_window_location_default_and_override(resolver):
    assert resolver.window_location("LAB1", None) == "LAB1"
    assert resolver.window_location("LAB1", "Narnia") == "LAB1"
    assert resolver.window_location("LAB1", "Node 2") == "NOD2"
    assert resolver.window_location("USOS", "LAB1P3") == "LAB1P3"


# ------------------------------------------------------------------------------------------------ reconciler location
def test_ac_per_11_sightings_use_camera_default_location(make_reconciler, inv):
    process(make_reconciler(camera_id="cam-nod2-aft"), [[("laptop-A", BOX_A)]], [obj("laptop", BOX_A)],
            {"laptop-A": "laptop"})
    assert inv.last["assignments"][0]["sighting"]["location_id"] == "NOD2"


def test_ac_per_11_location_hint_overrides_camera_default(make_reconciler, inv):
    process(make_reconciler(camera_id="cam-handheld"), [[("laptop-A", BOX_A)]], [obj("laptop", BOX_A)],
            {"laptop-A": "laptop"}, location_hint="Lab starboard rack 2")
    assert inv.last["assignments"][0]["sighting"]["location_id"] == "LAB1S2"


def test_ac_per_11_unmappable_hint_keeps_camera_default(make_reconciler, inv):
    process(make_reconciler(camera_id="cam-lab1-fwd"), [[("laptop-A", BOX_A)]], [obj("laptop", BOX_A)],
            {"laptop-A": "laptop"}, location_hint="behind the big white panel")
    assert inv.last["assignments"][0]["sighting"]["location_id"] == "LAB1"


def test_ac_per_11_camera_location_from_config(make_reconciler, inv, config):
    config.camera_locations["cam-test"] = "COL1A1"
    process(make_reconciler(camera_id="cam-test"), [[("laptop-A", BOX_A)]], [obj("laptop", BOX_A)],
            {"laptop-A": "laptop"})
    assert inv.last["assignments"][0]["sighting"]["location_id"] == "COL1A1"


def _no_capture_window(make_reconciler, *, camera_id="cam-handheld", hint="NOD2S5", events=()):
    frames = [[("laptop-A", BOX_A), ("orb-1", BOX_B)]]
    return process(make_reconciler(camera_id=camera_id), frames,
                   [obj("laptop", BOX_A), obj("glowing orb", BOX_B)], {"laptop-A": "laptop", "orb-1": "glowing orb"},
                   location_hint=hint, events=events)


def test_ac_per_11_no_capture_window_stores_no_frames_crops_or_raw(make_reconciler, inv, storage):
    _no_capture_window(make_reconciler)
    assert storage.objects == {}, list(storage.objects)
    p = inv.last
    for a in p["assignments"]:
        assert a["sighting"].get("frame_uri") is None
        assert (a.get("exemplar") or {}).get("crop_uri") is None
    assert p["window"].get("raw_uri") is None


def test_ac_per_11_no_capture_window_has_no_reviews(make_reconciler, inv):
    _no_capture_window(make_reconciler)
    assert inv.last.get("reviews", []) == []


def test_ac_per_11_no_capture_window_is_not_summarised(make_reconciler, inv):
    """AC-PER-43 (UI3-02) withdraws "summarised only": a private window keeps no summary text."""
    _no_capture_window(make_reconciler)
    assert inv.last["window"]["scene_summary"] is None
    assert inv.last["window"]["private"] is True


def test_ac_per_11_no_capture_events_carry_no_evidence(make_reconciler, inv, storage):
    ev = {"type": "used", "object_label": "laptop", "frame_index": 0, "confidence": 0.7}
    _no_capture_window(make_reconciler, events=[ev])
    assert all(e.get("evidence_uri") is None for e in inv.last.get("events", []))


def test_ac_per_11_no_capture_camera_default(make_reconciler, inv, storage, config):
    config.camera_locations["cam-cq"] = "NOD2P5"
    _no_capture_window(make_reconciler, camera_id="cam-cq", hint=None)
    assert storage.objects == {}
    assert inv.last.get("reviews", []) == []


# ------------------------------------------------------------------------------------------------ AC-PER-13 keys
def test_ac_per_13_frame_key_layout():
    t = datetime(2026, 10, 1, 12, 0, 5, 250000, tzinfo=UTC)
    k = frame_key("cam-lab1-fwd", t)
    assert re.fullmatch(r"frames/cam-lab1-fwd/2026-10-01/[0-9A-Za-z._-]+\.jpg", k), k
    assert "//" not in k and ":" not in k
    assert frame_key("cam-lab1-fwd", t) == k
    assert frame_key("cam-lab1-fwd", t.replace(microsecond=500000)) != k


def test_ac_per_13_crop_and_raw_key_layout():
    t = datetime(2026, 10, 1, 23, 59, 59, tzinfo=UTC)
    c0, c1 = crop_key("cam-nod2-aft", t, 0), crop_key("cam-nod2-aft", t, 1)
    assert c0.startswith("crops/cam-nod2-aft/2026-10-01/") and c0.endswith(".jpg") and c0 != c1
    r = raw_key("cam-nod2-aft", t)
    assert r.startswith("raw/cam-nod2-aft/2026-10-01/") and r.endswith(".json")


def test_ac_per_13_date_is_utc():
    from datetime import timedelta, timezone

    local = datetime(2026, 10, 2, 1, 0, 0, tzinfo=timezone(timedelta(hours=5)))  # 2026-10-01 20:00 UTC
    assert frame_key("cam-a", local).startswith("frames/cam-a/2026-10-01/")


def test_ac_per_13_window_uploads_frames_crops_and_raw(make_reconciler, inv, storage):
    frames = [[("laptop-A", BOX_A)], [("laptop-A", BOX_A)]]
    w = scene_window(frames)
    raw = '{"scene_summary": "raw reply text"}'
    omni = OmniResult(status="ok", observation=observation([obj("laptop", BOX_A, frame_index=1)]), raw=raw,
                      model="qwen-omni")
    make_reconciler().process(w, omni, detections_for(frames, {"laptop-A": "laptop"}))
    p = inv.last
    a = p["assignments"][0]
    fk = uri_key(a["sighting"]["frame_uri"])
    assert fk == frame_key("cam-lab1-fwd", w.frames[1].timestamp)
    assert storage.objects[fk][:2] == b"\xff\xd8" and storage.content_types[fk] == "image/jpeg"
    ck = uri_key(a["exemplar"]["crop_uri"])
    assert ck.startswith("crops/cam-lab1-fwd/2026-10-01/")
    assert storage.objects[ck][:2] == b"\xff\xd8"
    rk = uri_key(p["window"]["raw_uri"])
    assert rk.startswith("raw/cam-lab1-fwd/2026-10-01/")
    assert raw in storage.objects[rk].decode()
    assert p["window"]["model"] == "qwen-omni"


def test_ac_per_13_all_uris_point_at_configured_bucket(make_reconciler, inv, storage):
    process(make_reconciler(), [[("laptop-A", BOX_A), ("orb-1", BOX_B)]],
            [obj("laptop", BOX_A), obj("glowing orb", BOX_B)], {"laptop-A": "laptop", "orb-1": "glowing orb"})
    blob = json.dumps(inv.last)
    uris = re.findall(r"s3://[^\"]+", blob)
    assert uris and all(u.startswith(f"s3://{BUCKET}/") for u in uris)
    assert all(uri_key(u) in storage.objects for u in uris)


def test_ac_per_13_memory_storage_put_returns_uri(storage):
    uri = storage.put("frames/cam/2026-10-01/x.jpg", b"\xff\xd8data", "image/jpeg")
    assert uri == f"s3://{BUCKET}/frames/cam/2026-10-01/x.jpg"
    assert storage.objects["frames/cam/2026-10-01/x.jpg"] == b"\xff\xd8data"


def test_ac_per_13_s3_storage_uploads_with_content_type():
    with mock_aws():
        client = boto3.client("s3", region_name="us-east-1", aws_access_key_id="t", aws_secret_access_key="t")
        client.create_bucket(Bucket="frames-bucket")
        store = S3Storage(client, "frames-bucket")
        uri = store.put("raw/cam/2026-10-01/a.json", b'{"a": 1}', "application/json")
        assert uri == "s3://frames-bucket/raw/cam/2026-10-01/a.json"
        obj_ = client.get_object(Bucket="frames-bucket", Key="raw/cam/2026-10-01/a.json")
        assert obj_["Body"].read() == b'{"a": 1}'
        assert obj_["ContentType"] == "application/json"


def test_ac_per_13_s3_storage_from_config_env(monkeypatch):
    from perception.config import PerceptionConfig

    monkeypatch.setenv("S3_BUCKET", "cfg-bucket")
    monkeypatch.setenv("S3_ENDPOINT", "http://rustfs.local:9000")
    monkeypatch.setenv("S3_ACCESS_KEY", "k")
    monkeypatch.setenv("S3_SECRET_KEY", "s")
    cfg = PerceptionConfig.from_env()
    assert cfg.s3_bucket == "cfg-bucket" and cfg.s3_endpoint == "http://rustfs.local:9000"
    store = S3Storage.from_config(cfg)
    assert store.bucket == "cfg-bucket"
