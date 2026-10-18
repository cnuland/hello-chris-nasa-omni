"""AC-PER-33 (Gemma 4 A/B, 2026-10-18): Gemma 4 12B sometimes emits inverted boxes, e.g. "cabling"
[0.58, 0.1, 0.2, 0.91] (x2 < x1) or "hatch" [0.35, 0.55, 0.64, 0.54] (y2 < y1). Inverted boxes break IoU-based
consolidation and linking and give zero- or negative-area crops. interpret_reply normalises every object bbox:
swap inverted coordinates, clamp to [0, 1], and drop (set to null) boxes thinner than 2% of the frame either way.
The object itself is kept (it has no box)."""
import json

from perception.omni import interpret_reply


def _reply(boxes):
    return json.dumps({
        "scene_summary": "s", "location_hint": None, "people": [], "events": [], "transcript": "", "audio_events": [],
        "objects": [{"label": f"o{i}", "description": "d", "confidence": 0.8, "bbox": b, "frame_index": 0}
                    for i, b in enumerate(boxes)],
    })


def _boxes(raw):
    r = interpret_reply(raw)
    assert r.observation is not None, r.errors
    return [o.get("bbox") for o in r.observation["objects"]]


def test_inverted_x_is_swapped():
    assert _boxes(_reply([[0.58, 0.1, 0.2, 0.91]])) == [[0.2, 0.1, 0.58, 0.91]]


def test_inverted_y_is_swapped():
    assert _boxes(_reply([[0.35, 0.55, 0.64, 0.54]])) == [None]  # after the swap it is 0.01 tall: degenerate


def test_both_inverted_are_swapped():
    assert _boxes(_reply([[0.9, 0.8, 0.1, 0.2]])) == [[0.1, 0.2, 0.9, 0.8]]


def test_degenerate_box_becomes_null_but_object_kept():
    r = interpret_reply(_reply([[0.5, 0.5, 0.502, 0.9]]))
    assert r.observation["objects"][0]["bbox"] is None
    assert r.observation["objects"][0]["label"] == "o0"


def test_valid_boxes_unchanged():
    assert _boxes(_reply([[0.1, 0.2, 0.3, 0.4], None])) == [[0.1, 0.2, 0.3, 0.4], None]
