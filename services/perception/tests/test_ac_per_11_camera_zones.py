"""AC-PER-11 (Integration phase extension): camera-configured zones (ARCHITECTURE decision 5, "module -> zone
(camera-configured)").

A camera may carry zone rectangles (normalised frame coordinates). An object's location is the zone whose rectangle
contains the centre of its box, when that zone is a known location inside the window location and is not a
no_capture zone; otherwise it is the window location (camera default / location_hint), exactly as before.

Found by the eval integration run: without zones every sighting of a fixed lab camera is stored at module level
(LAB1), so the zone-level location accuracy gate (eval/thresholds.yaml location_acc >= 0.8) scored 0.0.
"""
from __future__ import annotations

import json

import pytest
from support import obj
from test_ac_per_08_reconciler import BOX_A, BOX_B, process

from perception.config import ConfigError, PerceptionConfig, parse_camera_zones

LAB_ZONES = {"LAB1S2": (0.0, 0.0, 0.5, 1.0), "LAB1P3": (0.5, 0.0, 1.0, 1.0)}


# ------------------------------------------------------------------------------------------------ config
def test_ac_per_11_camera_zones_default_empty():
    assert PerceptionConfig().camera_zones == {}
    assert PerceptionConfig.from_env({}).camera_zones == {}


def test_ac_per_11_camera_zones_from_env_json():
    raw = json.dumps({"cam-lab1-fwd": {"LAB1S2": [0, 0, 0.5, 1], "LAB1P3": [0.5, 0, 1, 1]}})
    cfg = PerceptionConfig.from_env({"CAMERA_ZONES": raw})
    assert cfg.camera_zones == {"cam-lab1-fwd": LAB_ZONES}


@pytest.mark.parametrize("raw", [
    "not json",
    "[]",
    json.dumps({"cam": [0, 0, 1, 1]}),                       # zones must be an object
    json.dumps({"cam": {"LAB1S2": [0, 0, 1]}}),              # 3 numbers
    json.dumps({"cam": {"LAB1S2": [0, 0, 1.5, 1]}}),         # outside 0..1
    json.dumps({"cam": {"LAB1S2": [0.6, 0, 0.5, 1]}}),       # x1 > x2
    json.dumps({"cam": {"LAB1S2": ["a", 0, 1, 1]}}),         # not numbers
])
def test_ac_per_11_camera_zones_malformed_rejected(raw):
    with pytest.raises(ConfigError):
        parse_camera_zones(raw)


# ------------------------------------------------------------------------------------------------ reconciler
def _rec(make_reconciler, camera_id="cam-lab1-fwd", zones=None):
    rec = make_reconciler(camera_id=camera_id)
    rec.config.camera_zones = {camera_id: dict(LAB_ZONES if zones is None else zones)}
    return rec


def test_ac_per_11_sighting_location_is_zone_containing_box_centre(make_reconciler, inv):
    rec = _rec(make_reconciler)
    process(rec, [[("laptop-A", BOX_A), ("drill-B", BOX_B)]], [obj("laptop", BOX_A), obj("drill", BOX_B)],
            {"laptop-A": "laptop", "drill-B": "drill"})
    locs = {a["new_entity"]["catalog_id"]: a["sighting"]["location_id"] for a in inv.last["assignments"]}
    assert locs == {"laptop": "LAB1S2", "drill-pistol": "LAB1P3"}


def test_ac_per_11_no_zone_contains_centre_falls_back_to_window_location(make_reconciler, inv):
    rec = _rec(make_reconciler, zones={"LAB1P3": (0.9, 0.9, 1.0, 1.0)})
    process(rec, [[("laptop-A", BOX_A)]], [obj("laptop", BOX_A)], {"laptop-A": "laptop"})
    assert inv.last["assignments"][0]["sighting"]["location_id"] == "LAB1"


def test_ac_per_11_zone_outside_window_location_is_ignored(make_reconciler, inv):
    """A location_hint moved the window to another module: the camera's LAB1 zones no longer apply."""
    rec = _rec(make_reconciler, camera_id="cam-handheld")
    process(rec, [[("laptop-A", BOX_A)]], [obj("laptop", BOX_A)], {"laptop-A": "laptop"}, location_hint="Node 2")
    assert inv.last["assignments"][0]["sighting"]["location_id"] == "NOD2"


def test_ac_per_11_unknown_zone_id_is_ignored(make_reconciler, inv):
    rec = _rec(make_reconciler, zones={"LAB1X9": (0.0, 0.0, 1.0, 1.0)})
    process(rec, [[("laptop-A", BOX_A)]], [obj("laptop", BOX_A)], {"laptop-A": "laptop"})
    assert inv.last["assignments"][0]["sighting"]["location_id"] == "LAB1"


def test_ac_per_11_no_capture_zone_is_never_assigned(make_reconciler, inv):
    """Fail closed: a private zone never becomes a sighting location of a capturing camera (the API would strip
    the frame and reject reviews, AC-API-19); the window location is used and nothing about privacy changes."""
    rec = _rec(make_reconciler, camera_id="cam-nod2-aft", zones={"NOD2S5": (0.0, 0.0, 1.0, 1.0)})
    process(rec, [[("laptop-A", BOX_A)]], [obj("laptop", BOX_A)], {"laptop-A": "laptop"})
    assert inv.last["assignments"][0]["sighting"]["location_id"] == "NOD2"


def test_ac_per_11_unknown_review_uses_object_zone(make_reconciler, inv):
    rec = _rec(make_reconciler)
    process(rec, [[("thing", BOX_B)]], [obj("glowing alien widget", BOX_B)], {"thing": "glowing alien widget"})
    reviews = inv.last["reviews"]
    assert [r["kind"] for r in reviews] == ["unknown_object"]
    assert reviews[0]["location_id"] == "LAB1P3"
    assert inv.last["assignments"][0]["sighting"]["location_id"] == "LAB1P3"
