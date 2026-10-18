"""AC-PER-64 structure memory (UI review round 6, finding UI6-05; inventory-api AC-API-46).

Live: the operator dismissed "insulated hose" (review 6388e99f, entity 35f41777, USOS, "thick white insulated hose
curving through the foreground") as Not inventory; the same duct came back as the unknown "white padded tube: a thick
white insulated tube running through the module" (93eee1d5, USOS, omni box only, no exemplar). The memory rows below
are what GET /structure-memory returns for that dismissal (inventory-api's tokeniser)."""
from __future__ import annotations

from datetime import timedelta

import pytest
from support import T0, detections_for, obj, observation, scene_window
from test_ac_per_08_reconciler import BOX_A, vec

from perception.config import ConfigError, PerceptionConfig
from perception.labels import content_tokens, head_family
from perception.types import OmniResult

HANDHELD = "cam-handheld"
TUBE = ("white padded tube", "a thick white insulated tube running through the module")


def memory(label="insulated hose", tokens=("insulated", "hose", "thick", "white", "curving", "foreground"),
           location="USOS", module=None, embedding=None, memory_id=1):
    return {"memory_id": memory_id, "review_id": f"00000000-0000-0000-0000-00000000000{memory_id}",
            "entity_id": None, "label": label, "label_key": label.replace(" ", "").lower(),
            "description_tokens": list(tokens), "location_id": location, "module_id": module,
            "embedding": [float(v) for v in embedding] if embedding is not None else None,
            "created_by": "ui-review-6", "created_at": "2026-10-18T08:16:55+00:00"}


def process(rec, objects, *, hint=None, t=0.0, frames=None, labels=None, people=()):
    frames = frames if frames is not None else [[]]
    w = scene_window(frames, t0=T0 + timedelta(seconds=t))
    omni = OmniResult(status="ok", observation=observation(objects, people=people, location_hint=hint),
                      model="fake-omni", raw="{}")
    out = rec.process(w, omni, detections_for(frames, labels or {}))
    assert out.status == "posted", out.error
    return out.payload


def tube(box=BOX_A, label=TUBE[0], description=TUBE[1]):
    return obj(label, box, description=description)


def unknown_reviews(p):
    return [r for r in p["reviews"] if r["kind"] == "unknown_object"]


# ================================================================================ real case
def test_live_case_white_padded_tube_after_insulated_hose_is_suppressed(make_reconciler, inv):
    inv.structure_memories = [memory()]
    p = process(make_reconciler(HANDHELD), [tube()])
    assert p["assignments"] == [] and unknown_reviews(p) == []
    assert p["window"]["structure_memory_suppressed"] == 1


def test_live_case_white_padded_tube_after_a_dismissed_duct_in_the_same_module(make_reconciler, inv):
    inv.structure_memories = [memory(label="duct", tokens=("duct", "white", "insulated", "thick", "ceiling"),
                                     location="JPM1", module="JPM1")]
    p = process(make_reconciler(HANDHELD), [tube()], hint="Kibo")
    assert p["assignments"] == [] and p["window"]["structure_memory_suppressed"] == 1


def test_without_memory_the_tube_is_still_raised(make_reconciler, inv):
    p = process(make_reconciler(HANDHELD), [tube()])
    assert len(unknown_reviews(p)) == 1 and p["window"]["structure_memory_suppressed"] == 0


def test_live_case_white_covering_is_not_the_dismissed_hatch_cover(make_reconciler, inv):
    """dc687b97 "hatch cover: white circular hatch cover partially open" was dismissed; 7d9d0b68 "white covering:
    large white fabric covering over equipment" is a different thing (same family, overlap 2/4 = 0.5 < 0.6)."""
    inv.structure_memories = [memory(label="hatch cover", tokens=("hatch", "cover", "white", "circular", "partially",
                                                                  "open"))]
    p = process(make_reconciler(HANDHELD), [obj("white covering", BOX_A,
                                                description="large white fabric covering over equipment")])
    assert len(unknown_reviews(p)) == 1


def test_different_family_is_never_suppressed_by_text(make_reconciler, inv):
    inv.structure_memories = [memory()]
    p = process(make_reconciler(HANDHELD), [obj("white padded sphere", BOX_A,
                                                description="a thick white insulated sphere in the module")])
    assert len(unknown_reviews(p)) == 1


# ================================================================================ place rules
def test_other_module_is_never_suppressed(make_reconciler, inv):
    inv.structure_memories = [memory(location="LAB1", module="LAB1", embedding=vec("tube-A"))]
    p = process(make_reconciler(HANDHELD), [tube(label="insulated hose")], hint="Columbus",
                frames=[[("tube-A", BOX_A)]], labels={"tube-A": "insulated hose"})
    assert len(unknown_reviews(p)) == 1


def test_nearby_segment_text_alone_is_not_enough(make_reconciler, inv):
    """A USOS sighting and a LAB1 memory: one lies inside the other, but text similarity alone is too weak there."""
    inv.structure_memories = [memory(location="LAB1", module="LAB1")]
    p = process(make_reconciler(HANDHELD), [tube()])
    assert len(unknown_reviews(p)) == 1


def test_nearby_segment_with_the_same_label_key_is_suppressed(make_reconciler, inv):
    inv.structure_memories = [memory(location="LAB1", module="LAB1")]
    p = process(make_reconciler(HANDHELD), [tube(label="Insulated-Hose")])
    assert p["assignments"] == [] and p["window"]["structure_memory_suppressed"] == 1


def test_module_sighting_and_segment_memory_with_the_same_label_key(make_reconciler, inv):
    inv.structure_memories = [memory()]  # USOS memory, LAB1 sighting
    p = process(make_reconciler(HANDHELD), [tube(label="insulated hose")], hint="Destiny")
    assert p["window"]["structure_memory_suppressed"] == 1


def test_other_segment_never_matches(make_reconciler, inv):
    inv.structure_memories = [memory(location="ROS")]
    p = process(make_reconciler(HANDHELD), [tube(label="insulated hose")])
    assert len(unknown_reviews(p)) == 1


# ================================================================================ embedding evidence
def test_embedding_in_the_auto_band_suppresses_whatever_the_label(make_reconciler, inv):
    inv.structure_memories = [memory(label="grey device", tokens=("grey", "device", "screen"), location="ZVEZDA",
                                     module="ZVEZDA", embedding=vec("dev-A"))]
    p = process(make_reconciler(HANDHELD), [obj("dark keypad", BOX_A, description="a dark keypad with buttons")],
                hint="Zvezda", frames=[[("dev-A", BOX_A)]], labels={"dev-A": "dark keypad"})
    assert p["assignments"] == [] and p["window"]["structure_memory_suppressed"] == 1


def test_embedding_below_the_auto_band_does_not_suppress(make_reconciler, inv):
    inv.structure_memories = [memory(label="grey device", tokens=("grey", "device"), location="ZVEZDA",
                                     module="ZVEZDA", embedding=vec("other-B"))]
    p = process(make_reconciler(HANDHELD), [obj("dark keypad", BOX_A, description="a dark keypad with buttons")],
                hint="Zvezda", frames=[[("dev-A", BOX_A)]], labels={"dev-A": "dark keypad"})
    assert len(unknown_reviews(p)) == 1


def test_embedding_match_nearby_segment_suppresses(make_reconciler, inv):
    inv.structure_memories = [memory(location="LAB1", module="LAB1", embedding=vec("tube-A"))]
    p = process(make_reconciler(HANDHELD), [tube()], frames=[[("tube-A", BOX_A)]],
                labels={"tube-A": "white padded tube"})
    assert p["window"]["structure_memory_suppressed"] == 1


# ================================================================================ scope
def test_catalogued_items_are_never_suppressed(make_reconciler, inv):
    inv.structure_memories = [memory(label="laptop", tokens=("laptop", "grey"), location="LAB1", module="LAB1",
                                     embedding=vec("laptop-A"))]
    p = process(make_reconciler(HANDHELD), [obj("laptop", BOX_A, description="grey laptop")], hint="Destiny",
                frames=[[("laptop-A", BOX_A)]], labels={"laptop-A": "laptop"})
    assert len(p["assignments"]) == 1 and p["window"]["structure_memory_suppressed"] == 0


def test_events_naming_a_suppressed_object_are_dropped(make_reconciler, inv):
    inv.structure_memories = [memory()]
    w = scene_window([[]])
    omni = OmniResult(status="ok", observation=observation(
        [tube()], events=[{"type": "used", "object_label": TUBE[0], "actor_ref": None, "confidence": 0.8}]),
        model="fake-omni", raw="{}")
    p = make_reconciler(HANDHELD).process(w, omni, [[]]).payload
    assert p["events"] == []


# ================================================================================ fetching
def test_memory_is_cached_and_refreshed(make_reconciler, inv):
    now = [0.0]
    rec = make_reconciler(HANDHELD)
    rec.clock = lambda: now[0]
    inv.structure_memories = []
    process(rec, [tube()])
    inv.structure_memories = [memory()]
    p = process(rec, [tube()], t=30)  # still the cached (empty) copy
    assert p["window"]["structure_memory_suppressed"] == 0
    now[0] = 61.0
    p = process(rec, [tube()], t=90)
    assert p["window"]["structure_memory_suppressed"] == 1
    assert inv.calls.count("list_structure_memory") == 2


def test_refresh_zero_turns_the_memory_off(make_reconciler, inv):
    inv.structure_memories = [memory()]
    p = process(make_reconciler(HANDHELD, structure_memory_refresh_s=0), [tube()])
    assert len(unknown_reviews(p)) == 1 and "list_structure_memory" not in inv.calls


def test_failing_fetch_keeps_the_previous_copy_and_still_posts(make_reconciler, inv):
    now = [0.0]
    rec = make_reconciler(HANDHELD)
    rec.clock = lambda: now[0]
    inv.structure_memories = [memory()]
    process(rec, [tube()])
    inv.structure_memories_error = RuntimeError("inventory-api down")
    now[0] = 120.0
    p = process(rec, [tube()], t=120)
    assert p["window"]["structure_memory_suppressed"] == 1


def test_client_without_the_method_means_no_memory(make_reconciler, inv):
    class Bare:
        def __getattr__(self, name):
            if name == "list_structure_memory":
                raise AttributeError(name)
            return getattr(inv, name)

    p = process(make_reconciler(HANDHELD, api=Bare()), [tube()])
    assert len(unknown_reviews(p)) == 1


def test_malformed_memory_rows_are_ignored(make_reconciler, inv):
    inv.structure_memories = [{"memory_id": 9}, "junk", memory(embedding=[0.0] * 3), memory(memory_id=2)]
    p = process(make_reconciler(HANDHELD), [tube()])
    assert p["window"]["structure_memory_suppressed"] == 1


# ================================================================================ helpers + config
@pytest.mark.parametrize(("a", "b"), [("white padded tube", "insulated hose"), ("duct", "pipe"),
                                      ("white covering", "hatch cover"), ("grey device", "display"),
                                      ("storage box", "equipment case")])
def test_head_families(a, b):
    assert head_family(a) == head_family(b)


def test_head_family_of_an_unlisted_noun_is_the_noun():
    assert head_family("orange sphere") == "sphere" and head_family("") is None


def test_content_tokens_fold_families_and_drop_placement_words():
    assert content_tokens(*TUBE) == {"white", "padded", "conduit", "thick", "insulated"}
    assert "module" not in content_tokens("module wall running through")


def test_config_from_env():
    cfg = PerceptionConfig.from_env({"PERCEPTION_STRUCTURE_MEMORY_REFRESH_S": "0",
                                     "PERCEPTION_STRUCTURE_MEMORY_TOKEN_FLOOR": "0.6"})
    assert cfg.structure_memory_refresh_s == 0 and cfg.structure_memory_token_floor == 0.6
    assert PerceptionConfig().structure_memory_refresh_s == 60 and PerceptionConfig().structure_memory_token_floor == 0.6
    with pytest.raises(ConfigError):
        PerceptionConfig.from_env({"PERCEPTION_STRUCTURE_MEMORY_TOKEN_FLOOR": "1.5"})


def test_http_client_reads_the_endpoint():
    import httpx

    from perception.inventory_client import HttpInventoryClient

    seen = []

    def handler(request):
        seen.append(str(request.url))
        return httpx.Response(200, json=[memory()])

    client = HttpInventoryClient("http://api", http_client=httpx.Client(transport=httpx.MockTransport(handler)))
    assert client.list_structure_memory(limit=7)[0]["label_key"] == "insulatedhose"
    assert seen == ["http://api/structure-memory?limit=7"]


def test_http_client_404_means_no_memory():
    import httpx

    from perception.inventory_client import HttpInventoryClient

    client = HttpInventoryClient("http://api", http_client=httpx.Client(
        transport=httpx.MockTransport(lambda r: httpx.Response(404, json={"detail": "Not Found"}))))
    assert client.list_structure_memory() == []
