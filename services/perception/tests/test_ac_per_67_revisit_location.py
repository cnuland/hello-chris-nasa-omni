"""AC-PER-67 second-pass locations (UI review round 6, finding UI6-03).

Measured on the live run (tests/fixtures/gemma4_tour_two_pass_2026-10-18.json, 63 windows, 3 passes of the looping tour)
and the DINOv2 frame embeddings of its stored frames (tests/fixtures/gemma4_tour_frame_embeddings_2026-10-18.json).
The current code reproduces every stored window location; pass 2 loses modules because each pass samples the title
captions 3.2 s later. The revisit location recovers frames that look like a window already placed in a module."""
from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np
import pytest
from support import LOCATIONS, T0, detections_for, obj, observation, scene_window

from perception.config import ConfigError, PerceptionConfig
from perception.locations import LocationResolver, ModuleCarry, RevisitLocator, caption_scene_change
from perception.types import OmniResult

FIX = Path(__file__).parent / "fixtures"
TWO_PASS = json.loads((FIX / "gemma4_tour_two_pass_2026-10-18.json").read_text())
EMBEDDINGS = json.loads((FIX / "gemma4_tour_frame_embeddings_2026-10-18.json").read_text())
HANDHELD = "cam-handheld"


def _replay(sim: float, margin: float = 0.04):
    """The run through the real window policy + carry + revisit locator (stored frames only)."""
    resolver = LocationResolver(LOCATIONS)
    carry = ModuleCarry(resolver, 45.0)
    locator = RevisitLocator()
    frames = {w["t_start"]: [np.asarray(f["embedding"], dtype=np.float32) for f in w["frames"]]
              for w in EMBEDDINGS["windows"]}
    out = []
    for w in sorted(TWO_PASS["windows"], key=lambda x: x["t_start"]):
        t_start, t_end = datetime.fromisoformat(w["t_start"]), datetime.fromisoformat(w["t_end"])
        if w["private"]:
            carry.step(t_start, t_end, resolver.window_policy("USOS", None, ""), no_capture=True, roaming=True)
            out.append((w, None, None))
            continue
        scope = resolver.window_policy("USOS", w["location_hint"], w["scene_summary"] or "")
        scene = caption_scene_change(resolver, w["location_hint"])
        c = carry.step(t_start, t_end, scope, no_capture=False, roaming=True, scene_change=scene)
        embs = frames.get(w["t_start"]) or []
        own = resolver.module_of(scope.location) if not c.carried and scope.source in ("hint", "summary") else None
        revisit = None
        if own and embs:
            locator.add(own, t_end, embs)
        elif resolver.module_of(c.location) is None and embs and not scope.elsewhere:
            def allowed(m, loc=c.location, mentions=frozenset(scope.mentions)):
                return (loc is None or resolver.within(m, loc)) and not (mentions - {m})

            hit = locator.query(embs, t_start, sim=sim, margin=margin, allowed=allowed)
            revisit = hit.module if hit else None
        out.append((w, c.location, revisit))
    return out


def test_fixture_reproduces_the_stored_locations():
    for w, location, _ in _replay(0.94):
        if not w["private"]:
            assert location == w["replay_current"]["location"], w["t_start"]


def test_per_pass_accuracy_before_the_revisit_location():
    """eval-real (AC-EVAL-10) per pass on the live run: 11/15, 6/15 (0 wrong), pass 3 0/6 so far."""
    scored: dict[int, list[bool]] = {}
    for w in TWO_PASS["windows"]:
        if w["caption_module"] and w["sighting_count"]:
            scored.setdefault(w["pass"], []).append(w["stored_location"] == w["caption_module"])
    assert (sum(scored[1]), len(scored[1])) == (11, 15)
    assert sum(scored[2]) == 6 and sum(scored[3]) == 0


def test_live_revisit_gains_are_right_and_nothing_is_wrong():
    hits = {w["t_start"][11:19]: (r, w["caption_module"]) for w, _, r in _replay(0.94) if r}
    assert hits == {"08:04:30": ("JPM1", "JPM1"), "08:19:00": ("COL1", "COL1")}


def test_lower_threshold_assigns_wrong_modules():
    """At 0.85 the run gets COL1 for the "NOD 3" Cupola window and two Kibo windows (and one in the Poisk stretch)."""
    wrong = [w["t_start"][11:19] for w, _, r in _replay(0.85) if r and r != w["caption_module"]]
    assert wrong


@pytest.mark.parametrize("sim", [0.9, 0.92, 0.94])
def test_no_assignment_in_unscored_stretches(sim):
    for w, _, r in _replay(sim):
        if r:
            assert w["caption_module"] == r, (w["t_start"], r)


# ================================================================================ locator unit
def _e(seed):
    v = np.random.default_rng(seed).normal(size=768).astype(np.float32)
    return v / np.linalg.norm(v)


def test_locator_requires_strictly_earlier_windows_threshold_and_margin():
    loc = RevisitLocator()
    a, b = _e(1), _e(2)
    loc.add("JPM1", T0, [a])
    loc.add("COL1", T0, [b])
    assert loc.query([a], T0 + timedelta(seconds=30), sim=0.94, margin=0.04).module == "JPM1"
    assert loc.query([a], T0 - timedelta(seconds=30), sim=0.94, margin=0.04) is None  # not earlier
    mix = (a + b) / np.linalg.norm(a + b)
    assert loc.query([mix], T0 + timedelta(seconds=30), sim=0.5, margin=0.04) is None  # tie: no margin
    assert loc.query([a], T0 + timedelta(seconds=30), sim=0.94, margin=0.04, allowed=lambda m: m != "JPM1") is None


def test_locator_gallery_is_bounded():
    loc = RevisitLocator(max_windows=2)
    for i in range(3):
        loc.add("JPM1" if i == 0 else "COL1", T0 + timedelta(seconds=i), [_e(i)])
    assert loc.query([_e(0)], T0 + timedelta(minutes=5), sim=0.94, margin=0.0) is None


# ================================================================================ reconciler integration
def process(rec, objects, *, hint=None, summary="crew in the module", t=0.0, frames=None, labels=None):
    frames = frames if frames is not None else [[("wall-K", (0.0, 0.0, 1.0, 1.0))]] * 3
    w = scene_window(frames, t0=T0 + timedelta(seconds=t))
    omni = OmniResult(status="ok", observation=observation(objects, location_hint=hint, scene_summary=summary),
                      model="fake-omni", raw="{}")
    out = rec.process(w, omni, detections_for(frames, labels) if labels else [[] for _ in frames])
    assert out.status == "posted", out.error
    return out.payload


def test_unplaced_window_that_looks_like_an_earlier_kibo_window_gets_kibo(make_reconciler, inv):
    rec = make_reconciler(HANDHELD)
    process(rec, [], hint='Japanese Experiment Module "Kibo"')
    p = process(rec, [obj("orange sphere", None, description="a small orange ball")], t=900)
    loc = p["window"]["location"]
    assert loc["location_id"] == "JPM1" and loc["source"] == "revisit" and loc["revisit_sim"] >= 0.94
    [a] = p["assignments"]
    assert a["sighting"]["location_id"] == "JPM1" and a["sighting"]["confidence"] <= 0.5


def test_different_view_stays_at_segment_level(make_reconciler, inv):
    rec = make_reconciler(HANDHELD)
    process(rec, [], hint="Kibo")
    p = process(rec, [], t=900, frames=[[("wall-C", (0.0, 0.0, 1.0, 1.0))]] * 3)
    assert p["window"]["location"]["location_id"] == "USOS"


def test_revisit_never_overrides_a_mention_of_another_module(make_reconciler, inv):
    rec = make_reconciler(HANDHELD)
    process(rec, [], hint="Kibo")
    p = process(rec, [], t=900, summary="a view from the Cupola into the station")
    assert p["window"]["location"]["source"] != "revisit"


def test_unresolvable_caption_does_not_block_the_visual_match(make_reconciler, inv):
    """Live 08:19:00: hint "To COL" (unresolvable, ends any carry) at the Columbus hatch; the frames match COL1."""
    rec = make_reconciler(HANDHELD)
    process(rec, [], hint="Columbus Laboratory Module")
    p = process(rec, [], t=900, hint="To COL")
    assert p["window"]["location"]["location_id"] == "COL1" and p["window"]["location"]["source"] == "revisit"


def test_revisit_window_is_neither_an_anchor_nor_a_gallery_entry_and_emits_no_move(make_reconciler, inv):
    rec = make_reconciler(HANDHELD)
    process(rec, [], hint="Kibo")
    eid = inv.add_entity(catalog_id="laptop", last_location="COL1", last_seen_at=T0 + timedelta(seconds=800))
    p = process(rec, [obj("laptop", None)], t=900)
    assert p["window"]["location"]["source"] == "revisit"
    assert [e for e in p["events"] if e["event_type"] == "moved"] == [] and eid
    p = process(rec, [], t=920)  # 20 s after the revisit window: not carried from it
    assert p["window"]["location"]["source"] in ("revisit", "camera")


def test_private_windows_never_join_the_gallery(make_reconciler, inv):
    rec = make_reconciler(HANDHELD)
    process(rec, [], hint="Harmony", summary="a crew member sleeping in a sleeping bag")
    p = process(rec, [], t=900)
    assert p["window"]["location"]["location_id"] == "USOS"


def test_revisit_off(make_reconciler, inv, embedder):
    rec = make_reconciler(HANDHELD, revisit_location_sim=0)
    calls = []
    original = embedder.embed
    embedder.embed = lambda crop: calls.append(1) or original(crop)
    process(rec, [], hint="Kibo")
    p = process(rec, [], t=900)
    assert p["window"]["location"]["location_id"] == "USOS" and calls == []


def test_fixed_cameras_do_not_use_it(make_reconciler, inv, embedder):
    calls = []
    original = embedder.embed
    embedder.embed = lambda crop: calls.append(1) or original(crop)
    process(make_reconciler("cam-lab1-fwd"), [], hint="Destiny")
    assert calls == []


def test_config():
    d = PerceptionConfig()
    assert (d.revisit_location_sim, d.revisit_location_margin) == (0.94, 0.04)
    c = PerceptionConfig.from_env({"PERCEPTION_REVISIT_LOCATION_SIM": "0", "PERCEPTION_REVISIT_LOCATION_MARGIN": "0.1"})
    assert (c.revisit_location_sim, c.revisit_location_margin) == (0, 0.1)
    with pytest.raises(ConfigError):
        PerceptionConfig.from_env({"PERCEPTION_REVISIT_LOCATION_SIM": "1.5"})
