"""Review round (AC-PER-58..63): fixes found by the adversarial review, each red first."""
from __future__ import annotations

import json
import math
from datetime import timedelta

import pytest
from support import LOCATIONS, T0, blend, obj, random_unit
from test_ac_per_08_reconciler import BOX_A, vec
from test_ac_per_60_63_reid_carry_events import HANDHELD, const, moved, process

from perception.labels import is_fixed_in_place, is_generic_label
from perception.locations import LocationResolver, caption_scene_change
from perception.omni import interpret_reply


# ---------------------------------------------------------------- P2: held candidates only in the ask band
def test_review_far_below_ask_band_cross_module_candidate_raises_no_review(make_reconciler, inv):
    inv.add_entity(catalog_id="laptop", embedding=random_unit(9), last_location="JPM1",
                   last_seen_at=T0 - timedelta(minutes=6))
    p = process(make_reconciler(HANDHELD, scorer=const(0.05)), [obj("laptop", BOX_A)], hint="Zvezda")
    assert p["assignments"][0]["entity_id"] is None
    assert [r for r in p["reviews"] if r["kind"] == "possible_duplicate"] == []


def test_review_held_review_does_not_block_same_place_adoption(make_reconciler, inv):
    near = inv.add_entity(catalog_id="laptop", last_location="ZVEZDA", last_seen_at=T0 - timedelta(minutes=2))
    inv.add_entity(catalog_id="laptop", embedding=blend(vec("laptop-A"), random_unit(3), 0.7),
                   last_location="JPM1", last_seen_at=T0 - timedelta(minutes=6))
    p = process(make_reconciler(HANDHELD, scorer=const(0.6)), [obj("laptop", BOX_A)], hint="Zvezda")
    assert str(p["assignments"][0]["entity_id"]) == near
    assert [r for r in p["reviews"] if r["kind"] == "possible_duplicate"] == []


# ---------------------------------------------------------------- P3: no synthesized move from inferred places
def test_review_no_synthesized_move_in_a_carried_window(make_reconciler, inv):
    rec = make_reconciler(HANDHELD, scorer=const(0.95))
    process(rec, [], hint="Zvezda", frames=[[]], labels={})  # anchor ZVEZDA
    inv.add_entity(catalog_id="laptop", embedding=vec("laptop-A"), last_location="JPM1",
                   last_seen_at=T0 - timedelta(minutes=6))
    p = process(rec, [obj("laptop", BOX_A)], t=20)
    assert p["window"]["location"]["source"] == "carried"
    assert moved(p) == []


# ---------------------------------------------------------------- P3: placeholder hints do not end the carry
@pytest.mark.parametrize("hint", ["unknown", "null", "N/A", "none", "ISS", "International Space Station",
                                  "station", "interior", "the space station", "Node", "module"])
def test_review_placeholder_hints_are_not_captions(hint):
    assert caption_scene_change(LocationResolver(LOCATIONS), hint) is None


def test_review_real_captions_still_count():
    r = LocationResolver(LOCATIONS)
    assert [caption_scene_change(r, h) for h in ("NOD 3", "To COL", "Ti LAB", "To FGB")] == ["caption"] * 4


# ---------------------------------------------------------------- P3: regex false positives
@pytest.mark.parametrize("text", ["floating free, not attached to the wall", "loose, not fixed to the rack",
                                  "no longer mounted on the wall"])
def test_review_negated_fixed_in_place(text):
    assert not is_fixed_in_place(text)


@pytest.mark.parametrize("label,description", [
    ("grey object", "the crew's grey object near the astronaut's arm"),
    ("grey object", "grey object, no text or markings"),
    ("grey object", "about 30 cm long"),
    ("grey device", "one of 2 grey devices"),
])
def test_review_weak_cues_are_not_distinguishing(label, description):
    assert is_generic_label(label, description)


@pytest.mark.parametrize("description", ["grey device labelled 'MSG'", "grey box with serial SN-2231",
                                         "grey unit marked A12"])
def test_review_real_identifiers_still_distinguish(description):
    assert not is_generic_label("grey device", description)


# ---------------------------------------------------------------- P3: non-finite coordinates
def _reply(box):
    return json.dumps({"scene_summary": "s", "location_hint": None, "people": [], "events": [], "transcript": "",
                       "audio_events": [], "objects": [{"label": "o", "description": "d", "confidence": 0.8,
                                                        "bbox": box, "frame_index": 0}]})


def test_review_infinite_coordinate_is_clamped_not_invalid():
    r = interpret_reply(_reply([0.1, 0.2, math.inf, 0.5]).replace("Infinity", "1e999"))
    assert r.observation is not None and r.observation["objects"][0]["bbox"] == [0.1, 0.2, 1.0, 0.5]


def test_review_nan_coordinate_makes_the_box_null():
    r = interpret_reply(_reply([0.1, 0.2, 0.3, 0.4]).replace("0.3", "NaN"))
    assert r.observation is not None and r.observation["objects"][0]["bbox"] is None


# ---------------------------------------------------------------- P3: same-class IoU links before cross-class ones
def test_review_unknown_listed_first_does_not_steal_a_catalog_box(make_reconciler):
    from support import scene_window

    from perception.types import Detection, OmniResult

    rec = make_reconciler()
    box = (0.30, 0.40, 0.50, 0.60)
    dets = [[Detection("laptop", (0.30, 0.40, 0.51, 0.61), 0.9)]]
    objs = [{"label": "grey slab", "description": "x", "confidence": 0.95, "bbox": list(box), "frame_index": 0},
            {"label": "laptop", "description": "x", "confidence": 0.9, "bbox": list(box), "frame_index": 0}]
    w = scene_window([[]], height=608, width=1080)
    omni = OmniResult(status="ok", observation={"scene_summary": "s", "location_hint": None, "objects": objs,
                      "people": [], "events": [], "transcript": "", "audio_events": []}, model="g", raw="{}")
    out = rec.process(w, omni, dets)
    by = {(a.get("new_entity") or {}).get("label"): a["sighting"]["bbox_source"] for a in out.payload["assignments"]}
    assert by["laptop"] == "detector" and by["grey slab"] == "omni"
