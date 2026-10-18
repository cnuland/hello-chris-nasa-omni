"""AC-PER-58 (live Gemma 4 run, 2026-10-18): Gemma 4 gives object boxes in its native box_2d format, [y1, x1, y2, x2]
on a 0..1000 grid, while the prompt asked for [x1, y1, x2, y2] normalised to 0..1. AC-PER-33 clamped them to [0, 1],
so every box became degenerate and was nulled: all 142 boxes of the run were lost.

Measured on the real replies (tests/fixtures/gemma4_boxes_2026-10-18.json): 34 replies, 142 boxes, all integers in
0..1000. Drawn on the real video frames, [y1,x1,y2,x2]/1000 lands on the object (treadmill belt, laptop, orange sphere,
wristwatch, camera) and [x1,y1,x2,y2]/1000 does not.

Rule: the box order is per model (OMNI_BOX_ORDER = auto | xyxy | yxyx; auto = yxyx for a Gemma model, else xyxy); the
scale is per reply (any coordinate > 1.5 -> the whole reply is on the 0..1000 grid); then the AC-PER-33 rules."""
import json
from pathlib import Path

import pytest

from perception.config import ConfigError, PerceptionConfig
from perception.omni import (
    OmniClient,
    box_order_for,
    build_system_prompt,
    create_omni_client,
    interpret_reply,
)
from perception.salvage import salvage_observation
from perception.schema import extract_first_json_object

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "gemma4_boxes_2026-10-18.json"
REAL = json.loads(FIXTURE.read_text())
W, H = 1080, 608  # tour-medium.mp4


def _reply(boxes):
    return json.dumps({
        "scene_summary": "s", "location_hint": None, "people": [], "events": [], "transcript": "", "audio_events": [],
        "objects": [{"label": f"o{i}", "description": "d", "confidence": 0.8, "bbox": b, "frame_index": 0}
                    for i, b in enumerate(boxes)],
    })


def _boxes(raw, **kw):
    r = interpret_reply(raw, **kw)
    assert r.observation is not None, r.errors
    return [o.get("bbox") for o in r.observation["objects"]]


def _raw_boxes():
    out = []
    for w in REAL["windows"]:
        obj = extract_first_json_object(w["raw"]) or salvage_observation(w["raw"])
        out += [o.get("bbox") for o in obj.get("objects") or []]
    return out


# ------------------------------------------------------------------------------------------------ measured format
def test_real_replies_are_all_on_the_0_1000_grid():
    boxes = _raw_boxes()
    assert len(boxes) == 142
    assert all(isinstance(v, int) and 0 <= v <= 1000 for b in boxes for v in b)
    assert any(v > 1.5 for b in boxes for v in b)


def _survivors(order):
    kept = total = 0
    for w in REAL["windows"]:
        r = interpret_reply(w["raw"], box_order=order, max_objects=40)
        assert r.observation is not None
        for o in r.observation["objects"]:
            total += 1
            kept += o.get("bbox") is not None
    return kept, total


def test_before_every_real_box_was_lost_with_the_old_clamp():
    """The old rule (AC-PER-33 alone): clamp the raw numbers to [0, 1]. Reproduced here to pin the 'before' figure."""
    def old(box):
        x1, y1, x2, y2 = (min(max(float(v), 0.0), 1.0) for v in box)
        x1, x2 = sorted((x1, x2))
        y1, y2 = sorted((y1, y2))
        return None if x2 - x1 < 0.02 or y2 - y1 < 0.02 else [x1, y1, x2, y2]

    survivors = [b for b in _raw_boxes() if old(b) is not None]
    # the only survivor is "green fabric cover" [0, 0, 800, 1000] clamped to the whole frame: wrong as well
    assert survivors == [[0, 0, 800, 1000]]
    assert old(survivors[0]) == [0.0, 0.0, 1.0, 1.0]


def test_after_every_real_box_survives_as_yxyx():
    kept, total = _survivors("yxyx")
    assert total == 142
    # one legitimately degenerate box: "drink pouch" [510, 388, 545, 405] is 1.7% of the frame wide (AC-PER-33)
    assert kept == 141


@pytest.mark.parametrize("check", REAL["order_checks"], ids=lambda c: c["label"])
def test_converted_real_boxes_are_xyxy_on_the_object(check):
    """The visually checked objects: the converted box is the yxyx reading, as canonical [x1, y1, x2, y2] 0..1."""
    y1, x1, y2, x2 = check["bbox"]
    [box] = _boxes(_reply([check["bbox"]]), box_order="yxyx")
    assert box == pytest.approx([x1 / 1000, y1 / 1000, x2 / 1000, y2 / 1000])


def test_treadmill_lands_on_its_belt():
    """Treadmill 063508: the belt is at px x 358..586, y 515..608 of the 1080x608 frame."""
    [box] = _boxes(_reply([[847, 331, 1000, 543]]), box_order="yxyx")
    px = [box[0] * W, box[1] * H, box[2] * W, box[3] * H]
    assert px == pytest.approx([357.5, 515.0, 586.4, 608.0], abs=1.0)


# ------------------------------------------------------------------------------------------------ scale and order
def test_grid_reply_in_xyxy_order_is_rescaled():
    [box] = _boxes(_reply([[100, 200, 300, 400]]))
    assert box == pytest.approx([0.1, 0.2, 0.3, 0.4])


def test_unit_reply_in_xyxy_order_is_unchanged():
    assert _boxes(_reply([[0.1, 0.2, 0.3, 0.4]]), box_order="xyxy") == [[0.1, 0.2, 0.3, 0.4]]


def test_unit_reply_in_yxyx_order_is_reordered():
    assert _boxes(_reply([[0.1, 0.2, 0.3, 0.4]]), box_order="yxyx") == [[0.2, 0.1, 0.4, 0.3]]


def test_scale_is_per_reply_not_per_box():
    """[0, 0, 1, 1] next to a grid box is a 1/1000 corner box (degenerate), not the whole frame."""
    first, second = _boxes(_reply([[100, 200, 300, 400], [0, 0, 1, 1]]), box_order="yxyx")
    assert first == pytest.approx([0.2, 0.1, 0.4, 0.3]) and second is None


def test_grid_values_above_1000_are_clamped():
    assert _boxes(_reply([[0, 0, 1200, 500]]), box_order="yxyx") == [[0.0, 0.0, 0.5, 1.0]]


def test_box_sanity_rules_still_apply_after_conversion():
    # inverted on the grid, then swapped; a 1/1000-thin box is degenerate
    first, second = _boxes(_reply([[400, 300, 200, 100], [500, 500, 505, 900]]), box_order="yxyx")
    assert first == pytest.approx([0.1, 0.2, 0.3, 0.4]) and second is None


def test_malformed_boxes_still_fail_validation():
    r = interpret_reply(_reply([[100, 200, 300]]), box_order="yxyx")
    assert r.observation is None


def test_unknown_box_order_is_rejected():
    with pytest.raises(ValueError):
        interpret_reply(_reply([]), box_order="xy")


# ------------------------------------------------------------------------------------------------ model / config
@pytest.mark.parametrize("model,order", [
    ("gemma4", "yxyx"), ("google/gemma-4-12B-it", "yxyx"), ("RedHatAI/Gemma-4-12B-it-FP8", "yxyx"),
    ("Qwen/Qwen2.5-Omni-7B", "xyxy"), ("omni", "xyxy"), ("", "xyxy"),
])
def test_auto_order_by_model(model, order):
    assert box_order_for(model, "auto") == order


def test_explicit_order_wins_over_the_model():
    assert box_order_for("gemma4", "xyxy") == "xyxy"
    assert box_order_for("Qwen/Qwen2.5-Omni-7B", "yxyx") == "yxyx"


def test_config_box_order_env():
    assert PerceptionConfig.from_env({}).omni_box_order == "auto"
    assert PerceptionConfig.from_env({"OMNI_BOX_ORDER": "YXYX"}).omni_box_order == "yxyx"
    with pytest.raises(ConfigError):
        PerceptionConfig.from_env({"OMNI_BOX_ORDER": "yx"})


def test_factory_passes_the_resolved_order():
    cfg = PerceptionConfig(omni_url="http://x", omni_model="gemma4")
    client = create_omni_client(cfg)
    assert isinstance(client, OmniClient) and client.box_order == "yxyx"
    cfg = PerceptionConfig(omni_url="http://x", omni_model="Qwen/Qwen2.5-Omni-7B")
    assert create_omni_client(cfg).box_order == "xyxy"
    cfg = PerceptionConfig(omni_url="http://x", omni_model="gemma4", omni_realtime=True)
    assert create_omni_client(cfg).box_order == "yxyx"


# ------------------------------------------------------------------------------------------------ prompt
def _prompt_schema(prompt):
    start = prompt.index("against this JSON schema: ") + len("against this JSON schema: ")
    obj, _ = json.JSONDecoder().raw_decode(prompt[start:])
    return obj["properties"]["objects"]["items"]["properties"]["bbox"]


def test_gemma_prompt_asks_for_its_native_box_2d_format():
    p = build_system_prompt(["laptop"], box_order="yxyx")
    assert "[y1,x1,y2,x2]" in p and "0..1000" in p
    assert "[x1,y1,x2,y2] normalised to 0..1" not in p
    bbox = _prompt_schema(p)
    assert bbox["items"]["maximum"] == 1000 and "y1" in bbox["description"]


def test_xyxy_prompt_is_unchanged():
    p = build_system_prompt(["laptop"])
    assert "bbox is [x1,y1,x2,y2] normalised to 0..1" in p
    assert _prompt_schema(p)["items"]["maximum"] == 1


def test_client_prompt_and_parser_use_the_same_order():
    from datetime import UTC, datetime

    import httpx
    import numpy as np

    from perception.types import Keyframe, Window

    seen = {}

    def handler(request):
        body = json.loads(request.content)
        seen["system"] = body["messages"][0]["content"]
        reply = _reply([[847, 331, 1000, 543]])
        return httpx.Response(200, json={"choices": [{"message": {"content": reply}, "finish_reason": "stop"}]})

    client = OmniClient("http://omni", model="gemma4", box_order="yxyx",
                        http_client=httpx.Client(transport=httpx.MockTransport(handler)))
    ts = datetime(2026, 10, 18, tzinfo=UTC)
    win = Window(t_start=ts, t_end=ts, frames=[Keyframe(timestamp=ts, image=np.zeros((8, 8, 3), np.uint8))])
    res = client.analyze(win)
    assert "[y1,x1,y2,x2]" in seen["system"]
    assert res.observation["objects"][0]["bbox"] == pytest.approx([0.331, 0.847, 0.543, 1.0])


# ------------------------------------------------------------------------------------------------ IoU link classes
# With Gemma's boxes restored, link pass 1 (IoU) fires. Measured with the real OWLv2 (CPU) on the real frames of the
# run: 39 IoU links, 29 of them to a detection of ANOTHER class (laptop -> "cable" 0.34, camera -> "cargo transfer
# bag" 0.61, food package -> "laptop" 0.48). A catalog instance's crop and exemplar must come from a box of its own
# class; an unknown (no class) may take another class's box only when the boxes clearly agree (IoU >= 0.5).
def _link_run(make_reconciler, objects, dets):
    from support import scene_window

    from perception.types import OmniResult

    rec = make_reconciler()
    w = scene_window([[] for _ in dets], height=608, width=1080)
    omni = OmniResult(status="ok", observation={
        "scene_summary": "s", "location_hint": None, "objects": objects, "people": [], "events": [],
        "transcript": "", "audio_events": []}, model="gemma4", raw="{}")
    out = rec.process(w, omni, dets)
    assert out.status == "posted", out.error
    return out.payload


def _o(label, box, conf=0.9):
    return {"label": label, "description": label, "confidence": conf, "bbox": list(box), "frame_index": 0}


def test_catalog_instance_never_takes_another_class_box_by_iou(make_reconciler):
    from perception.types import Detection

    laptop = (0.30, 0.40, 0.50, 0.60)
    dets = [[Detection("cable", (0.30, 0.40, 0.52, 0.62), 0.9)]]  # IoU ~0.83
    p = _link_run(make_reconciler, [_o("laptop", laptop)], dets)
    [a] = p["assignments"]
    assert a["sighting"]["bbox_source"] == "omni"
    assert a["sighting"]["bbox"] == pytest.approx(list(laptop))


def test_catalog_instance_takes_its_own_class_box_by_iou_over_a_better_other_class_box(make_reconciler):
    from perception.types import Detection

    own = (0.32, 0.42, 0.55, 0.65)
    dets = [[Detection("cable", (0.30, 0.40, 0.50, 0.60), 0.95), Detection("laptop", own, 0.4)]]
    p = _link_run(make_reconciler, [_o("laptop", (0.30, 0.40, 0.50, 0.60))], dets)
    [a] = p["assignments"]
    assert a["sighting"]["bbox_source"] == "detector" and a["sighting"]["bbox"] == pytest.approx(list(own))


def test_unknown_takes_another_class_box_only_when_boxes_clearly_agree(make_reconciler):
    from perception.types import Detection

    sphere = (0.53, 0.42, 0.56, 0.48)
    ball = (0.53, 0.42, 0.565, 0.485)  # the real case: "orange sphere" vs OWLv2 "ball", IoU 0.92 (here ~0.8)
    p = _link_run(make_reconciler, [_o("orange sphere", sphere)], [[Detection("ball", ball, 0.5)]])
    assert p["assignments"][0]["sighting"]["bbox_source"] == "detector"
    weak = (0.53, 0.42, 0.60, 0.50)  # IoU ~0.4: below the cross-class bar
    p = _link_run(make_reconciler, [_o("orange sphere", sphere)], [[Detection("ball", weak, 0.5)]])
    assert p["assignments"][0]["sighting"]["bbox_source"] == "omni"


def test_cross_class_iou_config():
    assert PerceptionConfig().link_iou_cross_class == 0.5
    assert PerceptionConfig.from_env({"PERCEPTION_LINK_IOU_CROSS_CLASS": "0.6"}).link_iou_cross_class == 0.6
    with pytest.raises(ConfigError):
        PerceptionConfig.from_env({"PERCEPTION_LINK_IOU_CROSS_CLASS": "1.5"})
