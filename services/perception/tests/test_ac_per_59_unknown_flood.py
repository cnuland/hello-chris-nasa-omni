"""AC-PER-59 (UI review round 5, finding UI5-02): the unknown queue flooded (5 -> 26 open reviews in 15 minutes) with
station structure and fixed equipment the AC-PER-50 denylist missed, and with generic colour+noun labels.

Real round-5 labels (docs/ui-review/round5/findings.json): "yellow latch: a yellow latch mechanism on the hatch frame",
"silver foil insulation: corrugated silver insulation along the floor", "silver cylinder: small silver cylinder mounted
on the wall", "white cloth cover: large white protective cover on equipment", "treadmill: a treadmill with a red belt",
"grey object: large, smooth grey object in the foreground" (the hatch).

Rules: more structure keys (latch, insulation, foil, treadmill, fixed exercise equipment); "cover" is structure as a
head noun after modifier words only ("white cloth cover", not "laptop cover"); an unknown described as fixed in place
("mounted on the wall", "attached to the hatch") is structure; a generic colour+noun label ("grey object", "silver
cylinder", "blue container") with no distinguishing description becomes an unknown only when the AC-PER-54 query of its
own label finds a detector box, otherwise it is counted and skipped; at most 2 unknowns per frame and 4 per window."""
from __future__ import annotations

import json
from datetime import timedelta
from pathlib import Path

import pytest
from support import CATALOG, T0, obj, observation, scene_window

from perception import UNKNOWN
from perception.labels import CatalogMapper, is_fixed_in_place, is_generic_label, is_structure_label
from perception.omni import interpret_reply
from perception.reconciler import structure_filtered
from perception.types import Detection, OmniResult

FIXTURES = Path(__file__).resolve().parent / "fixtures"
MAPPER = CatalogMapper(CATALOG)

ROUND5 = [  # (label, description) as stored live; all map to UNKNOWN
    ("yellow latch", "a yellow latch mechanism on the hatch frame"),
    ("silver foil insulation", "corrugated silver insulation along the floor"),
    ("silver cylinder", "small silver cylinder mounted on the wall"),
    ("white cloth cover", "large white protective cover on equipment"),
    ("treadmill", "a treadmill with a red belt"),
    ("grey object", "large, smooth grey object in the foreground"),
]
LOOSE = [  # real loose items the queue must keep
    ("orange sphere", "a small orange spherical object"),
    ("orange tool", "an orange handled tool"),
    ("white wrapped object", "large white bundle wrapped in protective material"),
    ("pen", "a black pen floating"),
    ("green bag", "green fabric bag"),  # AC-PER-69 amendment: the wristwatch is a worn item now (UI6-07)
]


def _o(label, description):
    return {"label": label, "description": description, "confidence": 0.9, "bbox": None, "frame_index": 0}


# ------------------------------------------------------------------------------------------------ labels
@pytest.mark.parametrize("label,description", ROUND5)
def test_round5_labels_map_to_unknown(label, description):
    assert MAPPER.map(label).catalog_id == UNKNOWN


@pytest.mark.parametrize("label,description", ROUND5[:5])
def test_round5_structure_is_filtered(label, description):
    assert structure_filtered([_o(label, description)], MAPPER) != []


def test_round5_grey_object_is_generic_not_structure():
    label, description = ROUND5[5]
    assert structure_filtered([_o(label, description)], MAPPER) == []
    assert is_generic_label(label, description)


@pytest.mark.parametrize("label,description", LOOSE)
def test_loose_items_are_kept(label, description):
    assert structure_filtered([_o(label, description)], MAPPER) == []
    assert not is_generic_label(label, description)


@pytest.mark.parametrize("label", ["latch", "yellow latch", "hatch latches", "insulation", "silver foil insulation",
                                   "foil", "treadmill", "exercise equipment", "exercise machine", "cycle ergometer",
                                   "white cloth cover", "green fabric cover", "protective cover", "white cover",
                                   "hatch frame"])
def test_new_structure_labels(label):
    assert is_structure_label(label)


@pytest.mark.parametrize("label", ["laptop cover", "lens cover", "camera cover", "memory module", "foil pouch",
                                   "orange sphere", "pen", "wristwatch", "tool bag with handle"])
def test_cover_after_an_item_noun_and_loose_items_are_not_structure(label):
    assert not is_structure_label(label)


@pytest.mark.parametrize("text", ["small silver cylinder mounted on the wall", "a box mounted to the rack",
                                  "grey unit mounted on a panel", "a latch attached to the hatch",
                                  "a yellow handle on the hatch frame", "gauge built into the wall",
                                  "a box bolted to the floor", "silver cylinder fixed to the wall"])
def test_fixed_in_place_descriptions(text):
    assert is_fixed_in_place(text)


@pytest.mark.parametrize("text", ["a white pouch attached to a rail", "a white pouch attached to a rack",
                                  "smaller white pouch attached to the ceiling with velcro",
                                  "a green storage bag attached to a rail", "a small orange spherical object",
                                  "black watch with a yellow face", "", None])
def test_stowed_loose_items_are_not_fixed_in_place(text):
    """Pouches and bags velcroed to a rail or rack are stowage, i.e. loose items (16 real listings in this run)."""
    assert not is_fixed_in_place(text)


@pytest.mark.parametrize("label,description", [
    ("grey object", "large smooth grey object"), ("white item", "a white item"), ("silver cylinder", "silver"),
    ("blue container", "blue storage container in the background"), ("grey device", "a large grey device"),
    ("black device", "a small black electronic device"), ("cylindrical container", "large silver container"),
    ("storage container", "large cylindrical silver container with white straps"), ("object", "an object"),
    ("the small white thing", ""),
])
def test_generic_labels(label, description):
    assert is_generic_label(label, description)


@pytest.mark.parametrize("label,description", [
    ("white box", "a large white rectangular container with some text on top"),  # text: distinguishing
    ("grey device", "grey device with label MSG-3"),
    ("silver cylinder", "silver cylinder with a serial number"),
    ("gas cylinder", "two yellow and blue cylinders secured together"),  # 'gas' is not a colour or size
    ("white pouch", "a white pouch"), ("black bag", "a large black bag"), ("yellow helmet", "yellow helmet"),
    ("white wrapped object", "large white bundle"), ("orange sphere", "orange ball"), ("spacesuit", "white suit"),
])
def test_not_generic_labels(label, description):
    assert not is_generic_label(label, description)


def test_real_run_unknown_listings_effect():
    """All 35 UNKNOWN listings (seed catalog) of the live run (tests/fixtures/gemma4_boxes_2026-10-18.json): before, 8
    were structure and 27 could reach the queue; after, 13 are structure, 6 generic listings (5 labels) are held for a
    detector box, and 16 listings (14 labels) can reach the queue (then capped per frame / window)."""
    d = json.loads((FIXTURES / "gemma4_boxes_2026-10-18.json").read_text())
    unknown = []
    for w in d["windows"]:
        for o in interpret_reply(w["raw"], box_order="yxyx", max_objects=40).observation["objects"]:
            if MAPPER.map(o["label"]).catalog_id == UNKNOWN:
                unknown.append(o)
    assert len(unknown) == 35
    structure = structure_filtered(unknown, MAPPER)
    rest = [o for o in unknown if o not in structure]
    generic = [o for o in rest if is_generic_label(o["label"], o.get("description"))]
    kept = sorted({o["label"] for o in rest if o not in generic})
    assert len(structure) == 13
    assert len(generic) == 6
    assert sorted({o["label"] for o in generic}) == [
        "black device", "blue container", "cylindrical container", "grey object", "storage container"]
    assert kept == ["black bag", "black strap", "gas cylinder", "green bag", "grey device", "orange bag",
                    "orange sphere", "orange tool", "spacesuit", "white box", "white covered object",
                    "white wrapped object", "wristwatch", "yellow helmet"]  # "grey device ... with a screen"
    assert len(rest) - len(generic) == 16


# ------------------------------------------------------------------------------------------------ reconciler
def run(rec, objects, *, qd=None, n_frames=1, t=0.0):
    w = scene_window([[] for _ in range(n_frames)], t0=T0 + timedelta(seconds=t), height=608, width=1080)
    omni = OmniResult(status="ok", observation=observation(objects), model="fake-omni", raw="{}")
    kwargs = {} if qd is None else {"query_detect": qd}
    out = rec.process(w, omni, [[] for _ in range(n_frames)], **kwargs)
    assert out.status == "posted", out.error
    return out.payload


def labels_of(payload):
    return sorted((a.get("new_entity") or {}).get("label") for a in payload["assignments"])


def unknown_reviews(payload):
    return [r for r in payload["reviews"] if r["kind"] == "unknown_object"]


class QD:
    def __init__(self, boxes):
        self.boxes = boxes

    def __call__(self, frame_idx, texts):
        return [Detection(t, b, 0.6) for t in texts for (f, b) in self.boxes.get(t, []) if f == frame_idx]


def test_round5_window_puts_only_loose_items_in_the_queue(make_reconciler):
    rec = make_reconciler()
    objects = [obj(lbl, description=d, frame_index=0) for lbl, d in ROUND5]
    p = run(rec, objects)
    assert labels_of(p) == []
    assert unknown_reviews(p) == []
    assert p["window"]["structure_filtered"] == 5
    assert p["window"]["generic_unknowns_skipped"] == 1


def test_generic_label_with_a_query_box_becomes_an_unknown(make_reconciler):
    rec = make_reconciler()
    qd = QD({"grey object": [(0, (0.3, 0.4, 0.6, 0.9))]})
    p = run(rec, [obj("grey object", description="large smooth grey object")], qd=qd)
    assert labels_of(p) == ["grey object"]
    assert p["window"]["generic_unknowns_skipped"] == 0
    [a] = p["assignments"]
    assert a["sighting"]["bbox_source"] == "detector"


def test_generic_label_without_a_query_box_is_skipped(make_reconciler):
    rec = make_reconciler()
    p = run(rec, [obj("grey object", description="large smooth grey object")], qd=QD({}))
    assert labels_of(p) == [] and p["window"]["generic_unknowns_skipped"] == 1


def test_loose_items_still_become_unknowns_without_a_box(make_reconciler):
    rec = make_reconciler()
    p = run(rec, [obj(lbl, description=d, frame_index=0) for lbl, d in LOOSE[:2]])
    assert labels_of(p) == ["orange sphere", "orange tool"]
    assert len(unknown_reviews(p)) == 2


def test_at_most_two_unknowns_per_frame(make_reconciler):
    rec = make_reconciler()
    objects = [obj(lbl, description=d, frame_index=0, confidence=c)
               for (lbl, d), c in zip(LOOSE, (0.6, 0.9, 0.7, 0.8, 0.5), strict=True)]
    p = run(rec, objects)
    assert labels_of(p) == ["orange tool", "pen"]  # the two most confident
    assert p["window"]["unknowns_capped"] == 3


def test_at_most_four_unknowns_per_window(make_reconciler):
    rec = make_reconciler()
    labels = ["orange sphere", "orange tool", "white wrapped object", "pen", "green bag", "red clip"]  # AC-PER-69
    objects = [obj(lbl, frame_index=i // 2, confidence=0.9 - i * 0.05) for i, lbl in enumerate(labels)]
    p = run(rec, objects, n_frames=3)
    assert labels_of(p) == sorted(labels[:4])
    assert p["window"]["unknowns_capped"] == 2


def test_catalog_objects_are_never_capped(make_reconciler):
    rec = make_reconciler()
    objects = [obj("laptop", (0.1 * i, 0.1, 0.1 * i + 0.08, 0.3), frame_index=0) for i in range(5)]
    objects += [obj(lbl, description=d) for lbl, d in LOOSE[:3]]
    p = run(rec, objects)
    assert [lbl for lbl in labels_of(p) if lbl != "laptop"] == ["orange sphere", "orange tool"]
    assert p["window"]["unknowns_capped"] == 1
