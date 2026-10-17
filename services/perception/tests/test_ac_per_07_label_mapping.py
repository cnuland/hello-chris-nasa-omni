"""AC-PER-7 Label -> catalog mapping: exact or synonym match first, then fuzzy and embedding similarity to catalog
names; below the floor (0.55) -> UNKNOWN; never creates entities keyed on free text alone."""
from __future__ import annotations

import numpy as np
import pytest
from support import CATALOG, FakeInventory, detections_for, obj, observation, random_unit, scene_window

from perception import UNKNOWN
from perception.labels import CatalogMapper
from perception.types import OmniResult

CATALOG_IDS = {c["catalog_id"] for c in CATALOG}


class DictTextEmbedder:
    """Text embedder fake: known phrases map to fixed vectors; unknown text -> its own random vector."""

    def __init__(self, table: dict[str, np.ndarray]):
        self.table = {k.lower(): v for k, v in table.items()}
        self.calls: list[str] = []

    def embed_text(self, text: str) -> np.ndarray:
        self.calls.append(text)
        key = text.strip().lower()
        if key in self.table:
            return self.table[key]
        return random_unit(abs(hash(key)) % (2**31))


@pytest.fixture()
def mapper():
    return CatalogMapper(CATALOG)


def test_ac_per_07_default_floor():
    assert CatalogMapper(CATALOG).floor == 0.55


@pytest.mark.parametrize("label,expected", [
    ("drill", "drill-pistol"),
    ("Drill", "drill-pistol"),
    ("  multimeter ", "multimeter"),
    ("laptop", "laptop"),
    ("astrobee robot", "astrobee"),
    ("food package", "food-pack"),
])
def test_ac_per_07_exact_class_name_match(mapper, label, expected):
    m = mapper.map(label)
    assert m.catalog_id == expected
    assert m.method == "exact"
    assert m.score == pytest.approx(1.0)


def test_ac_per_07_exact_on_shared_class_name_returns_one_of_the_classes(mapper):
    m = mapper.map("cargo transfer bag")
    assert m.catalog_id in {"ctb-half", "ctb-single"} and m.method == "exact"


@pytest.mark.parametrize("label,expected", [
    ("PGT", "drill-pistol"),
    ("power drill", "drill-pistol"),
    ("pistol grip tool", "drill-pistol"),
    ("ThinkPad", "laptop"),
    ("astronaut", "crew-member"),
    ("spanner", "wrench"),
    ("Fluke", "multimeter"),
    ("torch", "flashlight"),
])
def test_ac_per_07_synonym_match(mapper, label, expected):
    m = mapper.map(label)
    assert m.catalog_id == expected, (label, m)
    assert m.method == "synonym"


@pytest.mark.parametrize("label,expected", [
    ("multimetre", "multimeter"),
    ("flash light", "flashlight"),
    ("laptops", "laptop"),
    ("ducttape", "duct-tape"),
    ("headsets", "headset"),
])
def test_ac_per_07_fuzzy_match(mapper, label, expected):
    m = mapper.map(label)
    assert m.catalog_id == expected, (label, m)
    assert m.method == "fuzzy"
    assert 0.55 <= m.score < 1.0


@pytest.mark.parametrize("label", ["banana", "glowing orb", "zzzz", "", "   ", "the"])
def test_ac_per_07_below_floor_is_unknown(mapper, label):
    m = mapper.map(label)
    assert m.catalog_id == UNKNOWN
    assert m.score < 0.55


def test_ac_per_07_embedding_similarity_used_after_string_matching():
    drill_vec = random_unit(1)
    table = {"drill": drill_vec, "hand-held boring device": drill_vec}
    m = CatalogMapper(CATALOG, text_embedder=DictTextEmbedder(table)).map("hand-held boring device")
    assert m.catalog_id == "drill-pistol"
    assert m.method == "embedding"
    assert m.score >= 0.55


def test_ac_per_07_embedding_below_floor_is_unknown():
    m = CatalogMapper(CATALOG, text_embedder=DictTextEmbedder({})).map("glowing orb")
    assert m.catalog_id == UNKNOWN


def test_ac_per_07_exact_match_wins_over_embedding():
    emb = DictTextEmbedder({"drill": random_unit(5), "laptop": random_unit(5)})
    m = CatalogMapper(CATALOG, text_embedder=emb).map("laptop")
    assert m.catalog_id == "laptop" and m.method == "exact"


def test_ac_per_07_custom_floor_respected():
    strict = CatalogMapper(CATALOG, floor=0.99)
    assert strict.map("multimetre").catalog_id == UNKNOWN
    assert strict.map("multimeter").catalog_id == "multimeter"


@pytest.mark.parametrize("label", ["drill", "drillz", "new space gadget", "PGT", "xyz-123", "cable cable", "bag"])
def test_ac_per_07_only_catalog_ids_or_unknown_are_ever_returned(mapper, label):
    assert mapper.map(label).catalog_id in CATALOG_IDS | {UNKNOWN}


def test_ac_per_07_free_text_label_never_becomes_a_catalog_keyed_entity(make_reconciler, inv):
    """An omni label outside the catalog yields an `unknown` entity with catalog_id NULL - never a catalog id or a
    provisional entity keyed on the free text."""
    box = (0.2, 0.2, 0.5, 0.5)
    frames = [[("orb-1", box)]]
    w = scene_window(frames)
    omni = OmniResult(status="ok", observation=observation([obj("glowing orb", box)]), model="fake-omni")
    make_reconciler().process(w, omni, detections_for(frames, {"orb-1": "glowing orb"}))
    p = inv.last
    assert len(p["assignments"]) == 1
    ne = p["assignments"][0]["new_entity"]
    assert ne["catalog_id"] is None
    assert ne["status"] == "unknown"
    for a in p["assignments"]:
        if a.get("new_entity"):
            assert a["new_entity"]["catalog_id"] in CATALOG_IDS | {None}


def test_ac_per_07_reconciler_never_writes_catalog(make_reconciler):
    api = FakeInventory()
    box = (0.2, 0.2, 0.5, 0.5)
    frames = [[("orb-1", box)]]
    omni = OmniResult(status="ok", observation=observation([obj("mysterious widget", box)]), model="m")
    make_reconciler(api=api).process(scene_window(frames), omni, detections_for(frames, {"orb-1": "widget"}))
    assert set(api.calls) <= {"get_catalog", "get_locations", "candidates", "post_window", "count_live_entities",
                              "list_items", "patch_item"}
