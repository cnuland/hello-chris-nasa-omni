"""AC-PER-39..42 ISS module resolution (real-video eval 2026-10-18, docs/spec/20-perception.md).

The live Gemma 4 run scored window_location_acc 0.391 against the tour captions: every miss was a window left at
segment level. Official names ("Japanese Experiment Module "Kibo"") did not resolve, summaries that named the module
were not used, and a handheld camera's module was forgotten one window later.

- AC-PER-39 module aliases + mention scan (hints)
- AC-PER-40 scene-summary fallback for a roaming camera
- AC-PER-41 module carry for a roaming camera (PERCEPTION_MODULE_CARRY_S / _CONFIDENCE)
- AC-PER-42 real-data regression (tests/fixtures/gemma4_tour/*.json, tests/fixtures/real_tour_run_2026-10-18.json)
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from support import LOCATIONS, T0, observation, scene_window
from test_ac_per_08_reconciler import BOX_A

from perception.config import ConfigError, PerceptionConfig
from perception.locations import ISS_ALIASES, LocationResolver, ModuleCarry
from perception.omni import interpret_reply
from perception.types import OmniResult

FIXTURES = Path(__file__).resolve().parent / "fixtures"
RAW = FIXTURES / "gemma4_tour"
RUN = FIXTURES / "real_tour_run_2026-10-18.json"
SEED_IDS = {loc["location_id"] for loc in LOCATIONS}


@pytest.fixture()
def resolver():
    return LocationResolver(LOCATIONS)


def raw(name: str) -> dict:
    return json.loads((RAW / f"window_{name}.json").read_text())


# ================================================================================ AC-PER-39 aliases
@pytest.mark.parametrize("hint,expected", [
    ('Japanese Experiment Module "Kibo"', "JPM1"), ("Japanese Experiment Module 'Kibo'", "JPM1"),
    ("Japanese Experiment Module “Kibo”", "JPM1"), ("japanese experiment module", "JPM1"), ("JEM", "JPM1"),
    ("KIBO", "JPM1"), ("Kibo's lab", "JPM1"), ("Kibo JEM Pressurized Module", "JPM1"), ("JEM Pressurized Module", "JPM1"),
    ("Kibo Logistics Module", "JLP1"), ("JEM Logistics Module", "JLP1"), ("Experiment Logistics Module", "JLP1"),
    ("Zarya", "ZARYA"), ("FGB", "ZARYA"), ("Functional Cargo Block", "ZARYA"), ("Zarya FGB", "ZARYA"),
    ("Zarya Module", "ZARYA"), ("Nauka", "NAUKA"), ("MLM", "NAUKA"), ("Multipurpose Laboratory Module", "NAUKA"),
    ("Nauka Multipurpose Laboratory Module", "NAUKA"), ("Zvezda", "ZVEZDA"), ("Service Module", "ZVEZDA"),
    ("the service module", "ZVEZDA"), ("SM", "ZVEZDA"), ("Zvezda Service Module", "ZVEZDA"), ("Cupola", "CUP1"),
    ("Columbus", "COL1"), ("Columbus Laboratory", "COL1"), ("Columbus Lab", "COL1"), ("European laboratory", "COL1"),
    ("European Lab", "COL1"), ("Destiny", "LAB1"), ("US Lab", "LAB1"), ("U.S. Laboratory", "LAB1"),
    ("US Laboratory", "LAB1"), ("Destiny Laboratory Module", "LAB1"), ("Unity", "NOD1"), ("Node 1", "NOD1"),
    ("node one", "NOD1"), ("Harmony", "NOD2"), ("Node 2", "NOD2"), ("Tranquility", "NOD3"), ("Tranquillity", "NOD3"),
    ("Node 3", "NOD3"), ("Leonardo", "PMM1"), ("Permanent Multipurpose Module", "PMM1"), ("Unity Module", "NOD1"),
])
def test_ac_per_39_module_names_resolve_as_hints(resolver, hint, expected):
    assert resolver.resolve_hint(hint) == expected


@pytest.mark.parametrize("hint,expected", [
    ("Japanese Experiment Module", "JPM1"), ("Functional Cargo Block", "ZARYA"), ("Multipurpose Laboratory Module",
                                                                                  "NAUKA"),
    ("European laboratory", "COL1"), ("U.S. Laboratory", "LAB1"), ("SM", "ZVEZDA"), ("Service Module", "ZVEZDA"),
])
def test_ac_per_39_new_exact_aliases_also_serve_speech_and_events(resolver, hint, expected):
    assert resolver.resolve(hint) == expected


@pytest.mark.parametrize("hint", [
    "laboratory", "the laboratory", "laboratory module", "Laboratory", "service", "multipurpose", "multipurpose module",
    "experiment", "japanese experiment", "european", "US", "U.S.", "module", "node", "pressurized", "logistics",
    "To FGB", "to the Kibo", "a view from Unity into Harmony", "Pressurized Mating Adapter (PMA-2)",
    "Poisk Research Module", "Quest airlock", "the port", "deck",
])
def test_ac_per_39_generic_words_and_directions_do_not_resolve(resolver, hint):
    assert resolver.resolve_hint(hint) is None


def test_ac_per_39_aliases_only_name_seed_locations():
    assert set(ISS_ALIASES) <= SEED_IDS


def test_ac_per_39_no_invented_codes_for_modules_missing_from_the_seed():
    no_nauka = LocationResolver([loc for loc in LOCATIONS if loc["location_id"] != "NAUKA"])
    assert no_nauka.resolve_hint("Nauka Multipurpose Laboratory Module") is None
    assert no_nauka.scan_modules("A person works inside the Nauka module.").module is None


# ================================================================================ AC-PER-39 mention scan
@pytest.mark.parametrize("text,expected", [
    ('A person in a blue shirt floats in the Japanese Experiment Module "Kibo" while looking at equipment.', "JPM1"),
    ("Two astronauts are seen floating and interacting within the Unity Module of the ISS.", "NOD1"),
    ("A person in a blue suit works inside the Nauka Multipurpose Laboratory Module.", "NAUKA"),
    ("An astronaut in a blue shirt uses a camera to take photos through a Cupola window.", "CUP1"),
    ("A view from inside Unity into Harmony.", "NOD1"),
    ("An astronaut floats in Harmony looking toward the Destiny lab.", "NOD2"),
    ("A crew member in Kibo near the Quest airlock.", "JPM1"),
    ("Kibo’s robotic arm controls are visible.", "JPM1"),
    ("A person stows bags in the Kibo Logistics Module.", "JLP1"),
    ("The US Lab is crowded.", "LAB1"),
    ("A wide view of the Destiny Laboratory Module interior showing various equipment and a hatch.", "LAB1"),
])
def test_ac_per_39_scan_finds_the_module_the_camera_is_in(resolver, text, expected):
    assert resolver.scan_modules(text).module == expected


@pytest.mark.parametrize("text", [
    "A view from Unity into Harmony.",  # two modules, neither "in"
    "Kibo next to Harmony.",
    "A hatch leading to Kibo.",  # a single directional mention
    "A sign reads To FGB.",
    "An astronaut works in the lab.",  # "lab" alone is generic
    "Equipment in the laboratory.",
    "The crew works in harmony with the robot.",  # common word, not capitalised
    "They show us lab equipment.",  # "us" is not "US"
    "A beam of light crosses the module.",
    "A view into a cluttered ISS module showing various equipment and storage containers.",
    "A person floats in Kibo and in Harmony.",  # two "in" modules
    "A view between Harmony and Kibo.",  # review: "between", and "and" takes the previous marker
    "Equipment in Columbus and Harmony.",
    "",
])
def test_ac_per_39_scan_stays_unresolved_when_ambiguous_or_generic(resolver, text):
    assert resolver.scan_modules(text).module is None


@pytest.mark.parametrize("text", [
    "A person in a dark blue shirt works near a circular hatch in the Poisk Research Module.",
    "Astronauts prepare equipment in the Quest airlock.",
    "Cargo bags fill the Pressurized Mating Adapter.",
    "A view inside PMA-2.",
])
def test_ac_per_39_unseeded_module_is_elsewhere_not_a_location(resolver, text):
    result = resolver.scan_modules(text)
    assert result.module is None and result.elsewhere


def test_ac_per_39_seeded_module_not_elsewhere(resolver):
    result = resolver.scan_modules("A person floats in the Zvezda Service Module.")
    assert result.module == "ZVEZDA" and not result.elsewhere


# ================================================================================ AC-PER-40 summary fallback
KIBO_SUMMARY = 'A person in a blue shirt floats in the Japanese Experiment Module "Kibo" while looking at equipment.'


def test_ac_per_40_roaming_camera_takes_the_module_from_the_summary(resolver):
    scope = resolver.window_policy("USOS", None, KIBO_SUMMARY)
    assert (scope.location, scope.no_capture, scope.hint_conflict, scope.source) == ("JPM1", False, False, "summary")


def test_ac_per_40_unresolvable_hint_falls_back_to_the_summary(resolver):
    scope = resolver.window_policy("USOS", "To COL", KIBO_SUMMARY)
    assert (scope.location, scope.source) == ("JPM1", "summary")


def test_ac_per_40_resolvable_hint_wins_over_the_summary(resolver):
    scope = resolver.window_policy("USOS", "Harmony", KIBO_SUMMARY)
    assert (scope.location, scope.source) == ("NOD2", "hint")


def test_ac_per_40_summary_without_a_module_keeps_the_camera_location(resolver):
    scope = resolver.window_policy("USOS", None, "A view through a hatch into another module of the ISS.")
    assert (scope.location, scope.source) == ("USOS", "camera")


def test_ac_per_40_fixed_camera_ignores_the_summary(resolver):
    scope = resolver.window_policy("LAB1", None, "A view of the Columbus module through the hatch.")
    assert (scope.location, scope.hint_conflict, scope.source) == ("LAB1", False, "camera")


def test_ac_per_40_summary_module_must_lie_in_a_hinted_segment(resolver):
    assert resolver.window_policy("USOS", "Russian segment", "A person floats in the Zvezda module.").location == \
        "ZVEZDA"
    assert resolver.window_policy("USOS", "US segment", "A person floats in the Zvezda module.").location == "USOS"


def test_ac_per_40_unseeded_module_hint_or_summary_is_elsewhere(resolver):
    assert resolver.window_policy("USOS", "Poisk Research Module", None).elsewhere
    assert resolver.window_policy("USOS", None, "Work in the Poisk Research Module.").elsewhere
    assert not resolver.window_policy("USOS", "Harmony", "Work in the Poisk Research Module.").elsewhere


def test_ac_per_40_summary_naming_crew_quarters_stays_private(make_reconciler, inv, storage):
    window, _ = run_window(make_reconciler(camera_id="cam-handheld"), inv, 0,
                           summary="An astronaut in the Harmony module opens the crew quarters.")
    assert window["privacy"]["no_capture"] is True and storage.objects == {}
    assert window.get("raw_uri") is None


def test_ac_per_40_private_summary_module_makes_the_window_private():
    locs = [dict(loc) for loc in LOCATIONS]
    for loc in locs:
        if loc["location_id"] == "JPM1":
            loc["no_capture"] = True
    scope = LocationResolver(locs).window_policy("USOS", None, KIBO_SUMMARY)
    assert scope.no_capture is True


def test_ac_per_40_private_hint_is_not_rescanned(resolver):
    assert resolver.window_scope("LAB1", "Node 2 crew quarters") == ("LAB1", True)
    assert resolver.window_policy("USOS", "Node 2 crew quarters", KIBO_SUMMARY).no_capture is True


# ================================================================================ AC-PER-41 carry (reconciler)
def obj_laptop():
    return {"label": "laptop", "description": "black laptop", "confidence": 0.9, "bbox": list(BOX_A),
            "frame_index": 0}


def run_window(rec, inv, start_s: float, *, hint=None, summary="crew working in the module", length_s=5.0):
    w = scene_window([[("laptop-A", BOX_A)], [("laptop-A", BOX_A)]], t0=T0 + timedelta(seconds=start_s),
                     step_s=length_s)
    obs = observation([obj_laptop()], location_hint=hint, scene_summary=summary)
    rec.process(w, OmniResult(status="ok", observation=obs, raw="{}", model="fake"), [[], []])
    p = inv.last
    sightings = [a["sighting"] for a in p["assignments"]]
    return p["window"], sightings


def test_ac_per_41_kibo_unresolved_unresolved_harmony_carries(make_reconciler, inv):
    rec = make_reconciler(camera_id="cam-handheld")
    locs, sources, confs = [], [], []
    for start, hint in [(0, "Japanese Experiment Module 'Kibo'"), (15, None), (30, None), (45, "Harmony")]:
        window, sightings = run_window(rec, inv, start, hint=hint)
        locs.append(sightings[0]["location_id"])
        sources.append(window["location"]["source"])
        confs.append(sightings[0]["confidence"])
    assert locs == ["JPM1", "JPM1", "JPM1", "NOD2"]
    assert sources == ["hint", "carried", "carried", "hint"]
    assert confs == pytest.approx([0.9, 0.5, 0.5, 0.9])


def test_ac_per_41_carried_window_records_its_anchor_and_gap(make_reconciler, inv):
    rec = make_reconciler(camera_id="cam-handheld")
    run_window(rec, inv, 0, hint="Kibo")
    window, _ = run_window(rec, inv, 30)
    loc = window["location"]
    assert loc["location_id"] == "JPM1" and loc["source"] == "carried"
    assert loc["gap_s"] == pytest.approx(25.0)
    assert datetime.fromisoformat(loc["carried_from"]) == T0 + timedelta(seconds=5)


@pytest.mark.parametrize("start,carried", [(50, True), (50.001, False), (80, False)])
def test_ac_per_41_carry_horizon_is_measured_from_the_anchor(make_reconciler, inv, start, carried):
    rec = make_reconciler(camera_id="cam-handheld")
    run_window(rec, inv, 0, hint="Kibo")  # ends at 5 s
    _, sightings = run_window(rec, inv, start)
    assert (sightings[0]["location_id"] == "JPM1") is carried


def test_ac_per_41_carried_windows_do_not_extend_the_carry(make_reconciler, inv):
    rec = make_reconciler(camera_id="cam-handheld")
    run_window(rec, inv, 0, hint="Kibo")
    assert run_window(rec, inv, 30)[1][0]["location_id"] == "JPM1"
    assert run_window(rec, inv, 60)[1][0]["location_id"] == "USOS"  # 55 s after the last real evidence


def test_ac_per_41_summary_resolved_window_is_an_anchor(make_reconciler, inv):
    rec = make_reconciler(camera_id="cam-handheld")
    window, _ = run_window(rec, inv, 0, summary=KIBO_SUMMARY)
    assert window["location"]["source"] == "summary"
    assert run_window(rec, inv, 30)[1][0]["location_id"] == "JPM1"


def test_ac_per_41_resolved_window_wins_over_the_carry(make_reconciler, inv):
    rec = make_reconciler(camera_id="cam-handheld")
    run_window(rec, inv, 0, hint="Kibo")
    window, sightings = run_window(rec, inv, 10, summary="An astronaut floats in the Harmony module.")
    assert sightings[0]["location_id"] == "NOD2" and window["location"]["source"] == "summary"
    assert sightings[0]["confidence"] == pytest.approx(0.9)


def test_ac_per_41_never_carries_into_a_private_window(make_reconciler, inv, storage):
    rec = make_reconciler(camera_id="cam-handheld")
    run_window(rec, inv, 0, hint="Kibo")
    window, sightings = run_window(rec, inv, 10, summary="A crew member sleeping in a sleeping bag.")
    assert window["privacy"]["no_capture"] is True
    assert window["location"]["source"] != "carried" and sightings[0]["location_id"] != "JPM1"


def test_ac_per_41_never_carries_out_of_a_private_window(make_reconciler, inv):
    rec = make_reconciler(camera_id="cam-handheld", privacy_hold_s=0.0)
    run_window(rec, inv, 0, hint="Kibo")
    run_window(rec, inv, 10, summary="Somebody opens the crew quarters.")
    window, sightings = run_window(rec, inv, 20)
    assert "privacy" not in window
    assert sightings[0]["location_id"] == "USOS" and window["location"]["source"] == "camera"


def test_ac_per_41_private_window_with_its_own_module_is_not_an_anchor(make_reconciler, inv):
    rec = make_reconciler(camera_id="cam-handheld", privacy_hold_s=0.0)
    run_window(rec, inv, 0, hint="Harmony", summary="A person sleeping in the Harmony module.")
    assert run_window(rec, inv, 10)[1][0]["location_id"] == "USOS"


def test_ac_per_41_unseeded_module_breaks_the_carry(make_reconciler, inv):
    rec = make_reconciler(camera_id="cam-handheld")
    run_window(rec, inv, 0, hint="Nauka Multipurpose Laboratory Module")
    window, sightings = run_window(rec, inv, 10, hint="Poisk Research Module")
    assert sightings[0]["location_id"] == "USOS" and window["location"]["source"] == "camera"
    assert run_window(rec, inv, 20)[1][0]["location_id"] == "USOS"


def test_ac_per_41_segment_hint_blocks_a_carry_from_the_other_segment(make_reconciler, inv):
    rec = make_reconciler(camera_id="cam-handheld")
    run_window(rec, inv, 0, hint="Kibo")
    assert run_window(rec, inv, 10, hint="Russian segment")[1][0]["location_id"] == "ROS"
    rec = make_reconciler(camera_id="cam-handheld")
    run_window(rec, inv, 0, hint="Zvezda")
    assert run_window(rec, inv, 10, hint="Russian segment")[1][0]["location_id"] == "ZVEZDA"


def test_ac_per_41_fixed_camera_never_carries(make_reconciler, inv):
    rec = make_reconciler(camera_id="cam-lab1-fwd")
    run_window(rec, inv, 0, hint="Columbus")
    window, sightings = run_window(rec, inv, 10)
    assert sightings[0]["location_id"] == "LAB1" and window["location"]["source"] == "camera"


def test_ac_per_41_carry_can_be_turned_off(make_reconciler, inv):
    rec = make_reconciler(camera_id="cam-handheld", module_carry_s=0.0)
    run_window(rec, inv, 0, hint="Kibo")
    assert run_window(rec, inv, 10)[1][0]["location_id"] == "USOS"


def test_ac_per_41_carry_confidence_is_configurable(make_reconciler, inv):
    rec = make_reconciler(camera_id="cam-handheld", module_carry_confidence=0.3)
    run_window(rec, inv, 0, hint="Kibo")
    assert run_window(rec, inv, 10)[1][0]["confidence"] == pytest.approx(0.3)


def test_ac_per_41_window_before_the_anchor_is_not_carried(make_reconciler, inv):
    rec = make_reconciler(camera_id="cam-handheld")
    run_window(rec, inv, 100, hint="Kibo")
    assert run_window(rec, inv, 50)[1][0]["location_id"] == "USOS"


def test_ac_per_41_overlapping_window_counts_as_gap_zero(make_reconciler, inv):
    rec = make_reconciler(camera_id="cam-handheld")
    run_window(rec, inv, 0, hint="Kibo", length_s=6)
    window, sightings = run_window(rec, inv, 4)
    assert sightings[0]["location_id"] == "JPM1" and window["location"]["gap_s"] == 0.0


def test_ac_per_41_module_carry_unit():
    r = LocationResolver(LOCATIONS)
    carry = ModuleCarry(r, carry_s=45.0)
    kibo = r.window_policy("USOS", "Kibo")
    d = carry.step(T0, T0 + timedelta(seconds=5), kibo, no_capture=False, roaming=True)
    assert (d.location, d.carried) == ("JPM1", False)
    blank = r.window_policy("USOS", None)
    d = carry.step(T0 + timedelta(seconds=20), T0 + timedelta(seconds=25), blank, no_capture=False, roaming=True)
    assert (d.location, d.carried, d.gap_s) == ("JPM1", True, 15.0)
    d = carry.step(T0 + timedelta(seconds=30), T0 + timedelta(seconds=35), blank, no_capture=True, roaming=True)
    assert (d.location, d.carried) == ("USOS", False)
    d = carry.step(T0 + timedelta(seconds=40), T0 + timedelta(seconds=45), blank, no_capture=False, roaming=True)
    assert (d.location, d.carried) == ("USOS", False)


# ================================================================================ AC-PER-41 config
def test_ac_per_41_config_defaults():
    c = PerceptionConfig()
    assert (c.module_carry_s, c.module_carry_confidence) == (45.0, 0.5)


def test_ac_per_41_config_from_env():
    c = PerceptionConfig.from_env({"PERCEPTION_MODULE_CARRY_S": "20", "PERCEPTION_MODULE_CARRY_CONFIDENCE": "0.4"})
    assert (c.module_carry_s, c.module_carry_confidence) == (20.0, 0.4)
    assert PerceptionConfig.from_env({"PERCEPTION_MODULE_CARRY_S": "0"}).module_carry_s == 0.0


@pytest.mark.parametrize("var,value", [
    ("PERCEPTION_MODULE_CARRY_S", "-1"), ("PERCEPTION_MODULE_CARRY_S", "soon"), ("PERCEPTION_MODULE_CARRY_S", "nan"),
    ("PERCEPTION_MODULE_CARRY_S", "inf"), ("PERCEPTION_MODULE_CARRY_S", "601"),
    ("PERCEPTION_MODULE_CARRY_CONFIDENCE", "1.5"), ("PERCEPTION_MODULE_CARRY_CONFIDENCE", "-0.1"),
])
def test_ac_per_41_config_rejects_bad_values(var, value):
    with pytest.raises(ConfigError):
        PerceptionConfig.from_env({var: value})


# ================================================================================ AC-PER-42 real data
@pytest.mark.parametrize("name,expected,elsewhere", [
    ("030318", "COL1", False),  # hint 'Columbus Laboratory Module'
    ("030348", "JPM1", False),  # hint 'Japanese Experiment Module "Kibo"' (was unresolved)
    ("030418", "USOS", False),  # no hint, "a view through a hatch into another module"
    ("030548", "NOD1", False),  # 'Unity Module'
    ("030618", "CUP1", False),  # 'Cupola'
    ("030648", "USOS", False),  # hint 'NOD 3' (signage), summary without module
    ("030948", "ZVEZDA", False),  # 'Zvezda Service Module'
    ("031018", "USOS", False),  # treadmill, no module named
    ("031118", "NAUKA", False),  # 'Nauka Multipurpose Laboratory Module' (was unresolved)
    ("032218", "USOS", False),  # hint 'To FGB' is signage pointing elsewhere
    ("032248", "USOS", True),  # 'Pressurized Mating Adapter (PMA-2)': not in the seed
    ("032648", "USOS", True),  # 'Poisk Research Module': not in the seed
])
def test_ac_per_42_real_gemma_replies_resolve(resolver, name, expected, elsewhere):
    obs = raw(name)
    scope = resolver.window_policy("USOS", obs.get("location_hint"), obs.get("scene_summary"))
    assert scope.location == expected and scope.elsewhere is elsewhere and scope.no_capture is False


def test_ac_per_42_real_kibo_summary_alone_resolves(resolver):
    obs = raw("030348")
    assert resolver.window_policy("USOS", None, obs["scene_summary"]).location == "JPM1"


def test_ac_per_42_real_replies_through_the_reconciler_carry_kibo(make_reconciler, inv):
    """Kibo reply, then the next live window (30 s later, no hint, no module named) inherits Kibo."""
    rec = make_reconciler(camera_id="cam-handheld")
    for i, name in enumerate(["030348", "030418"]):
        w = scene_window([[]] * 6, t0=T0 + timedelta(seconds=30 * i), step_s=1.0, index=i)
        text = (RAW / f"window_{name}.json").read_text()
        reply = interpret_reply(text)  # the live client's parsing (boxes sanitised, AC-PER-33)
        assert reply.observation is not None, reply.errors
        result = OmniResult(status="ok", observation=reply.observation, raw=text, model="google/gemma-4")
        rec.process(w, result, [[] for _ in range(6)])
        loc = inv.last["window"]["location"]
        assert loc["location_id"] == "JPM1"
        assert loc["source"] == ("hint" if i == 0 else "carried")


def replay_run(windows: list[dict], carry_s: float = 45.0) -> list[str]:
    """The live run through window_policy + ModuleCarry (cam-handheld). Private windows stored no raw reply, so
    their hint is unknown (None); their summary is stored."""
    r = LocationResolver(LOCATIONS)
    carry = ModuleCarry(r, carry_s=carry_s)
    predicted = []
    for w in windows:
        scope = r.window_policy("USOS", w["location_hint"], w["scene_summary"])
        d = carry.step(datetime.fromisoformat(w["t_start"]), datetime.fromisoformat(w["t_end"]), scope,
                       no_capture=w["no_capture"], roaming=True)
        predicted.append(r.module_of(d.location) or "unresolved")
    return predicted


def accuracy(windows, predicted) -> tuple[int, int]:
    scored = [(w["truth"], p) for w, p in zip(windows, predicted, strict=True) if w["truth"]]
    return sum(t == p for t, p in scored), len(scored)


def test_ac_per_42_real_run_fixture_is_the_measured_baseline():
    windows = json.loads(RUN.read_text())["windows"]
    assert len(windows) == 51
    old = [w["old_predicted"] for w in windows]
    assert accuracy(windows[:41], old[:41]) == (9, 23)  # the 0.391 report (41 windows)


def test_ac_per_42_real_run_replay_predicts_the_caption_modules():
    windows = json.loads(RUN.read_text())["windows"]
    predicted = replay_run(windows)
    assert accuracy(windows[:41], predicted[:41]) == (16, 23)  # 0.391 -> 0.696 on the exported 41 windows
    assert accuracy(windows, predicted) == (19, 30)  # all 51 windows of the run so far
    wrong = [(i, w["truth"], p) for i, (w, p) in enumerate(zip(windows, predicted, strict=True))
             if w["truth"] and p not in (w["truth"], "unresolved")]
    assert wrong == []  # never the wrong module: misses stay unresolved
    by_truth: dict[str, list[str]] = {}
    for w, p in zip(windows[:41], predicted[:41], strict=True):
        if w["truth"]:
            by_truth.setdefault(w["truth"], []).append(p)
    assert by_truth["JPM1"] == ["JPM1", "JPM1", "unresolved", "unresolved"]
    assert by_truth["NAUKA"] == ["NAUKA", "NAUKA"]
    assert by_truth["CUP1"] == ["CUP1", "CUP1", "CUP1", "CUP1"]
    assert by_truth["ZVEZDA"] == ["ZVEZDA", "ZVEZDA", "unresolved"]


def test_ac_per_42_chained_carry_would_assign_wrong_modules():
    """Why the carry is anchored to real evidence: chaining window to window carries NOD3 ('NoD3', unscored) across
    nine unnamed windows into the next pass's Columbus and Kibo windows."""
    windows = json.loads(RUN.read_text())["windows"]
    r = LocationResolver(LOCATIONS)
    module, prev_end, chained = None, None, []
    for w in windows:
        scope = r.window_policy("USOS", w["location_hint"], w["scene_summary"])
        start, end = datetime.fromisoformat(w["t_start"]), datetime.fromisoformat(w["t_end"])
        own = r.module_of(scope.location)
        if w["no_capture"] or scope.elsewhere:
            module = None
        elif own:
            module = own
        elif module and prev_end and (start - prev_end).total_seconds() <= 45:
            own = module
        prev_end = end
        chained.append(own or "unresolved")
    assert any(w["truth"] and p not in (w["truth"], "unresolved")
               for w, p in zip(windows, chained, strict=True))


# ================================================================================ review round (AC-PER-39..42)
@pytest.mark.parametrize("text", [
    "Inside the node, one crew member floats.",  # R1: no match across punctuation, "one" is not a node number
    "In the node, two astronauts unpack bags.",
    "the node; three bags",
    "Inside the node one crew member floats.",
])
def test_review_node_number_never_spans_punctuation_or_counts(resolver, text):
    assert resolver.scan_modules(text).module is None


def test_review_kibo_sentence_then_logistics_is_not_jlp(resolver):
    assert resolver.scan_modules("Work in Kibo. Logistics Module storage is full.").module == "JPM1"


def test_review_us_dot_lab_still_matches_across_abbreviation_dots(resolver):
    assert resolver.scan_modules("A crew member floats in the U.S. Lab.").module == "LAB1"


@pytest.mark.parametrize("text", [
    "A person in the JEM Experiment Logistics Module.",  # R2: the longer JLP name wins over a JEM prefix
    "Bags inside Kibo's Experiment Logistics Module.",
    "Bags inside Kibo's logistics module.",
    "A view in the ELM-PS.",
])
def test_review_jlp_official_names_resolve_to_jlp(resolver, text):
    assert resolver.scan_modules(text).module == "JLP1"


def test_review_carry_never_into_a_module_made_private_by_a_refresh():
    """R3: the anchor module is re-checked against the current resolver (catalog refresh)."""
    r = LocationResolver(LOCATIONS)
    carry = ModuleCarry(r, 45.0)
    carry.step(T0, T0 + timedelta(seconds=5), r.window_policy("USOS", "Columbus"), no_capture=False, roaming=True)
    locs = [dict(loc, no_capture=True) if loc["location_id"] == "COL1" else loc for loc in LOCATIONS]
    carry.resolver = LocationResolver(locs)
    blank = carry.resolver.window_policy("USOS", None)
    d = carry.step(T0 + timedelta(seconds=10), T0 + timedelta(seconds=15), blank, no_capture=False, roaming=True)
    assert (d.location, d.carried) == ("USOS", False)
    carry = ModuleCarry(r, 45.0)
    carry.step(T0, T0 + timedelta(seconds=5), r.window_policy("USOS", "Columbus"), no_capture=False, roaming=True)
    carry.resolver = LocationResolver([loc for loc in LOCATIONS if loc["location_id"] not in ("COL1", "COL1A1")])
    d = carry.step(T0 + timedelta(seconds=10), T0 + timedelta(seconds=15), blank, no_capture=False, roaming=True)
    assert (d.location, d.carried) == ("USOS", False)


@pytest.mark.parametrize("summary", [
    "A view into the hatchway between Harmony and Kibo.",  # R4: contrary evidence blocks the carry
    "A view of Kibo from the Cupola.",
    "Cargo moved toward the Quest airlock.",
])
def test_review_carry_blocked_by_another_module_mentioned(make_reconciler, inv, summary):
    rec = make_reconciler(camera_id="cam-handheld")
    run_window(rec, inv, 0, hint="Columbus")
    window, sightings = run_window(rec, inv, 10, summary=summary)
    assert sightings[0]["location_id"] == "USOS" and window["location"]["source"] == "camera"


def test_review_carry_allowed_when_only_the_anchor_module_is_mentioned_directionally(make_reconciler, inv):
    rec = make_reconciler(camera_id="cam-handheld")
    run_window(rec, inv, 0, hint="Columbus")
    assert run_window(rec, inv, 10, summary="A crew member points toward Columbus.")[1][0]["location_id"] == "COL1"


@pytest.mark.parametrize("camera,hint,expected", [
    ("NOD2", "Destiny visible through the hatch", "NOD2"),  # R5: fixed cameras do not scan hints
    ("NOD2", "Kibo hatch", "NOD2"),
    ("LAB1", "Harmony through hatch", "LAB1"),
    ("LAB1", "Columbus", "COL1"),  # exact hints still apply (AC-PER-23, flagged as a conflict)
])
def test_review_fixed_camera_hint_is_not_scanned(resolver, camera, hint, expected):
    assert resolver.window_policy(camera, hint).location == expected


@pytest.mark.parametrize("text", [
    "An astronaut floats outside Kibo.",  # R6: not-inside markers
    "A crew member moves out of Kibo.",
    "A crew member works near Columbus.",
    "Bags are stowed behind Destiny's rack.",
    "An astronaut floats in front of Kibo.",
    "A view of the Kibo hatch.",
    "Bags past the Cupola.",
])
def test_review_not_inside_markers_do_not_resolve(resolver, text):
    assert resolver.scan_modules(text).module is None


@pytest.mark.parametrize("text", [
    "Unity is important here.",  # R7: ordinary words, even capitalised, need module context
    "Harmony between crew members.",
    "A Columbus Day poster.",
    "Leonardo DiCaprio poster on the wall.",
    "Drawn in Leonardo da Vinci's style.",
])
def test_review_common_word_names_need_module_context(resolver, text):
    assert resolver.scan_modules(text).module is None


@pytest.mark.parametrize("text,expected", [
    ("Two astronauts float within the Unity Module of the ISS.", "NOD1"),
    ("An astronaut floats in Harmony.", "NOD2"),
    ("The Destiny lab is busy.", "LAB1"),
    ("Columbus module racks.", "COL1"),
    ("UNITY NODE hatch label.", "NOD1"),
])
def test_review_common_word_names_with_module_context_resolve(resolver, text, expected):
    assert resolver.scan_modules(text).module == expected


def test_review_multi_word_unseeded_name_ignores_case(resolver):
    """R8: "the quest airlock" (lower case) still places the camera elsewhere."""
    assert resolver.scan_modules("Suits are prepared in the quest airlock.").elsewhere == "Quest"


@pytest.mark.parametrize("hint", ["Harmony CQ", "CQ in Harmony", "quarters in Harmony", "Node 3 hygiene area"])
def test_review_more_private_terms(resolver, hint):
    assert resolver.is_private_hint(hint) is True
    assert resolver.window_policy("USOS", hint).no_capture is True


def test_review_programmatic_infinite_carry_is_off():
    r = LocationResolver(LOCATIONS)
    carry = ModuleCarry(r, float("inf"))
    carry.step(T0, T0 + timedelta(seconds=5), r.window_policy("USOS", "Kibo"), no_capture=False, roaming=True)
    d = carry.step(T0 + timedelta(hours=5), T0 + timedelta(hours=5, seconds=5), r.window_policy("USOS", None),
                   no_capture=False, roaming=True)
    assert d.carried is False
