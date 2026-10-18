"""AC-PER-24..28 omni output budget and truncation salvage (cluster integration 2026-10-17).

Measured: ~1,000 generated tokens per window and 34 % of replies cut off at max_tokens = 1500, each then retried and
dropped. AC-PER-24 prompt budget, AC-PER-25 config, AC-PER-26 salvage of a cut-off reply (complete elements only,
fail closed on the location hint, no retry), AC-PER-27 label-diverse client-side object cap, AC-PER-28 usage logging.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path

import httpx
import pytest
from support import detections_for, obj, observation, scene_window
from test_ac_per_03_omni_client import CLASS_NAMES, VALID, Recorder, system_text, window
from test_ac_per_08_reconciler import BOX_A, process
from test_ac_per_09_10_fungible_events import food_frames, food_objects

from perception import omni as omni_mod
from perception.config import ConfigError, PerceptionConfig
from perception.fakes import FakeOmni
from perception.omni import OmniClient, build_system_prompt, create_omni_client
from perception.realtime import RealtimeOmniClient
from perception.schema import validate_observation
from perception.types import OmniResult

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "omni_iss_tour_window0.json"
REAL = json.loads(FIXTURE.read_text())


def salvage_observation(text: str):
    from perception.salvage import salvage_observation as salvage

    return salvage(text)


def cap_objects(objects, max_objects):
    return omni_mod.cap_objects(objects, max_objects)


def interpret_reply(raw, **kw):
    return omni_mod.interpret_reply(raw, **kw)


def compact(o) -> str:
    return json.dumps(o, separators=(",", ":"))


def envelope(content: str, finish_reason: str = "stop", usage: dict | None = None,
             model: str = "qwen-omni") -> httpx.Response:
    doc = {"id": "x", "model": model, "object": "chat.completion",
           "choices": [{"index": 0, "message": {"role": "assistant", "content": content},
                        "finish_reason": finish_reason}]}
    if usage is not None:
        doc["usage"] = usage
    return httpx.Response(200, json=doc)


def budget_client(rec: Recorder, **kw) -> OmniClient:
    return OmniClient("http://omni.test", model="qwen-omni", catalog_names=CLASS_NAMES,
                      http_client=httpx.Client(transport=httpx.MockTransport(rec)), **kw)


# ================================================================================ AC-PER-24 prompt budget
def test_ac_per_24_prompt_states_object_cap_and_salience_order():
    p = build_system_prompt(["laptop"], max_objects=12).lower()
    assert "at most 12 objects" in p
    assert "most salient" in p and "inventory" in p
    assert "at most 7 objects" in build_system_prompt(["laptop"], max_objects=7).lower()


def test_ac_per_24_prompt_default_cap_is_12():
    assert "at most 12 objects" in build_system_prompt(["laptop"]).lower()


def test_ac_per_24_prompt_caps_descriptions_attributes_people_events():
    p = build_system_prompt(["laptop"]).lower()
    assert "12 words or fewer" in p
    assert "attributes only when" in p
    assert "at most 8 people" in p
    assert "at most 8 events" in p


def test_ac_per_24_prompt_asks_for_compact_json_without_prose():
    p = build_system_prompt(["laptop"]).lower()
    assert "compact json" in p
    assert "single line" in p
    assert "no indentation" in p
    assert "no text before or after" in p
    assert "no code fences" in p


def test_ac_per_24_prompt_puts_location_hint_first_and_always():
    p = build_system_prompt(["laptop"]).lower()
    assert "scene_summary, location_hint, objects, people, events, transcript, audio_events" in p
    assert "always include location_hint" in p


def test_ac_per_24_prompt_transcript_empty_when_asr_supplies_it():
    p = build_system_prompt(["laptop"], asr_transcript=True).lower()
    assert 'transcript must be ""' in p
    assert "verbatim" not in p


def test_ac_per_24_prompt_transcript_without_asr_is_verbatim_or_empty():
    p = build_system_prompt(["laptop"], asr_transcript=False).lower()
    assert "verbatim" in p
    assert '"" when nothing intelligible is said' in p
    assert "no speech detected" in p  # named as the placeholder NOT to write


def test_ac_per_24_client_prompt_uses_its_cap_and_asr_setting():
    rec = Recorder(envelope(compact(VALID)))
    budget_client(rec, max_objects=5, asr_transcript=True).analyze(window())
    text = system_text(rec.body()).lower()
    assert "at most 5 objects" in text
    assert 'transcript must be ""' in text


def test_ac_per_24_realtime_prompt_uses_the_same_budget():
    rt = RealtimeOmniClient("ws://x", model="m", catalog_names=["laptop"], max_objects=6, asr_transcript=True)
    instructions = rt._events(window(audio=False), None)[0]["session"]["instructions"].lower()
    assert "at most 6 objects" in instructions
    assert 'transcript must be ""' in instructions


# ================================================================================ AC-PER-25 config
def test_ac_per_25_config_defaults():
    cfg = PerceptionConfig.from_env({})
    assert cfg.omni_max_tokens == 700
    assert cfg.omni_max_objects == 12
    assert cfg.omni_repetition_penalty == pytest.approx(1.05)
    assert cfg.omni_truncated_confidence == pytest.approx(0.5)


def test_ac_per_25_config_from_env():
    cfg = PerceptionConfig.from_env({"OMNI_MAX_TOKENS": "512", "OMNI_MAX_OBJECTS": "8",
                                     "OMNI_REPETITION_PENALTY": "1.0", "OMNI_TRUNCATED_CONFIDENCE": "0.3"})
    assert (cfg.omni_max_tokens, cfg.omni_max_objects) == (512, 8)
    assert cfg.omni_repetition_penalty == 1.0
    assert cfg.omni_truncated_confidence == pytest.approx(0.3)


@pytest.mark.parametrize("var,value", [
    ("OMNI_MAX_TOKENS", "lots"), ("OMNI_MAX_TOKENS", "0"), ("OMNI_MAX_TOKENS", "-5"), ("OMNI_MAX_TOKENS", "7.5"),
    ("OMNI_MAX_TOKENS", "40000"),
    ("OMNI_MAX_OBJECTS", "0"), ("OMNI_MAX_OBJECTS", "41"), ("OMNI_MAX_OBJECTS", "x"),
    ("OMNI_REPETITION_PENALTY", "0"), ("OMNI_REPETITION_PENALTY", "-1"), ("OMNI_REPETITION_PENALTY", "2.5"),
    ("OMNI_REPETITION_PENALTY", "nan"),
    ("OMNI_TRUNCATED_CONFIDENCE", "1.5"), ("OMNI_TRUNCATED_CONFIDENCE", "-0.1"),
])
def test_ac_per_25_config_rejects_bad_values(var, value):
    with pytest.raises(ConfigError, match=var):
        PerceptionConfig.from_env({var: value})


def test_ac_per_25_create_omni_client_passes_budget_settings():
    cfg = PerceptionConfig(omni_url="http://omni", omni_max_tokens=640, omni_max_objects=9,
                           omni_repetition_penalty=1.1, asr_url="http://asr")
    c = create_omni_client(cfg)
    assert isinstance(c, OmniClient)
    assert (c.max_tokens, c.max_objects, c.repetition_penalty, c.asr_transcript) == (640, 9, 1.1, True)
    assert create_omni_client(PerceptionConfig(omni_url="http://omni")).asr_transcript is False


def test_ac_per_25_create_realtime_client_passes_prompt_settings():
    cfg = PerceptionConfig(omni_url="ws://omni", omni_realtime=True, omni_max_objects=9, asr_url="http://asr")
    c = create_omni_client(cfg)
    assert isinstance(c, RealtimeOmniClient)
    assert (c.max_objects, c.asr_transcript) == (9, True)


def test_ac_per_25_request_body_has_max_tokens_and_repetition_penalty():
    rec = Recorder(envelope(compact(VALID)))
    budget_client(rec, max_tokens=700, repetition_penalty=1.05).analyze(window())
    body = rec.body()
    assert body["max_tokens"] == 700
    assert body["repetition_penalty"] == pytest.approx(1.05)
    assert body["temperature"] == 0


def test_ac_per_25_client_defaults_are_the_budget_defaults():
    rec = Recorder(envelope(compact(VALID)))
    budget_client(rec).analyze(window())
    assert rec.body()["max_tokens"] == 700
    assert rec.body()["repetition_penalty"] == pytest.approx(1.05)


def test_ac_per_25_repetition_penalty_one_is_not_sent():
    rec = Recorder(envelope(compact(VALID)))
    budget_client(rec, repetition_penalty=1.0).analyze(window())
    assert "repetition_penalty" not in rec.body()


# ================================================================================ AC-PER-26 salvage (real data)
def _layout(o: dict) -> tuple[str, list[tuple[int, int]]]:
    """Compact serialisation of ``o`` and the [start, end) offsets of each objects[] element."""
    head = compact({k: o[k] for k in ("scene_summary", "location_hint")})[:-1] + ',"objects":['
    spans, pos, parts = [], len(head), []
    for i, el in enumerate(o["objects"]):
        s = compact(el)
        spans.append((pos, pos + len(s)))
        parts.append(s)
        pos += len(s) + (1 if i < len(o["objects"]) - 1 else 0)
    rest = {k: o[k] for k in ("people", "events", "transcript", "audio_events")}
    text = head + ",".join(parts) + "]," + compact(rest)[1:]
    assert text == compact(o), "layout must match json.dumps"
    return text, spans


def _cut(text: str, offset: int) -> str:
    return text.encode("utf-8")[:offset].decode("utf-8", errors="ignore")


REAL_TEXT, REAL_SPANS = _layout(REAL)


def _real_cuts():
    s3, e3 = REAL_SPANS[3]
    s5, _ = REAL_SPANS[5]
    s7, _ = REAL_SPANS[7]
    _, e4 = REAL_SPANS[4]
    desc = REAL_TEXT.index('"description"', s3) + len('"description":"') + 5
    label5 = REAL_TEXT.index('"label":"', s5) + len('"label":"laptop",')
    bbox7 = REAL_TEXT.index('"bbox":[', s7) + len('"bbox":[0.21,0.3')
    return {
        "mid-string (objects[3].description)": (desc, 3),
        "mid-object (objects[5] after its label)": (label5, 5),
        "mid-array (objects[7].bbox)": (bbox7, 7),
        "just after objects[4]'s closing brace": (e4, 5),
        "just after the comma following objects[4]": (e4 + 1, 5),
        "just before objects[3]'s closing brace": (e3 - 1, 3),
        "first object cut": (REAL_SPANS[0][0] + 10, 0),
    }


@pytest.mark.parametrize("name", list(_real_cuts()))
def test_ac_per_26_salvage_real_reply_keeps_exactly_the_complete_objects(name):
    offset, n_complete = _real_cuts()[name]
    salvaged = salvage_observation(_cut(REAL_TEXT, offset))
    assert salvaged is not None, name
    assert validate_observation(salvaged) == [], name
    assert salvaged["objects"] == REAL["objects"][:n_complete], name
    assert salvaged["scene_summary"] == REAL["scene_summary"]
    assert salvaged["location_hint"] == REAL["location_hint"]
    assert (salvaged["people"], salvaged["events"], salvaged["transcript"], salvaged["audio_events"]) == (
        [], [], "", [])


def test_ac_per_26_salvage_every_offset_of_the_real_reply_is_valid_or_none():
    """Exhaustive: every byte cut of the real reply salvages to a schema-valid prefix (or nothing)."""
    for offset in range(len(REAL_TEXT)):
        salvaged = salvage_observation(_cut(REAL_TEXT, offset))
        if salvaged is None:
            continue
        assert validate_observation(salvaged) == [], offset
        n = len(salvaged["objects"])
        assert salvaged["objects"] == REAL["objects"][:n], offset
        assert all(end <= offset for _s, end in REAL_SPANS[:n]), offset
        assert salvaged["events"] == REAL["events"][: len(salvaged["events"])], offset


def test_ac_per_26_salvage_in_people_events_and_transcript():
    t = REAL_TEXT
    # cut inside the second event: people complete, one event
    second_event = t.index('{"type":"used","object_label":"headset"')
    s = salvage_observation(t[: second_event + 20])
    assert s["objects"] == REAL["objects"] and s["people"] == REAL["people"] and s["events"] == REAL["events"][:1]
    # cut inside the transcript: everything else before it is kept, transcript is ""
    tr = t.index('"transcript":"') + len('"transcript":"No spe')
    s = salvage_observation(t[:tr])
    assert s["events"] == REAL["events"] and s["transcript"] == "" and s["audio_events"] == []
    # cut inside audio_events: the transcript is complete and kept
    s = salvage_observation(t[: t.index('"audio_events":[') + 25])
    assert s["transcript"] == REAL["transcript"] and s["audio_events"] == []


def test_ac_per_26_salvage_with_prose_and_code_fence_before_the_json():
    text = "Here is the observation:\n```json\n" + REAL_TEXT[: REAL_SPANS[2][1] + 1]
    s = salvage_observation(text)
    assert s is not None and s["objects"] == REAL["objects"][:3]


def test_ac_per_26_salvage_pretty_printed_reply():
    text = json.dumps(REAL, indent=2)
    cut = text.index('"frame_index": 2')
    s = salvage_observation(text[:cut])
    assert s is not None and s["objects"] == REAL["objects"][:4]


@pytest.mark.parametrize("text", [
    "",
    "no json at all",
    '{"scene_summary":"A pers',                       # summary cut
    '{"scene_summary":"A person in the lab.","loc',   # cut before the location hint completes (privacy)
    '{"scene_summary":"A person in the lab.","location_hint":"Node 2 crew quar',
    '{"scene_summary":"A person in the lab.","objects":[{"label":"laptop","description":"x","confidence":0.9}',
    '{"scene_summary":"","location_hint":null,"objects":[{"label":"lap',  # nothing useful
    '{"scene_summary":"A person.","location_hint":null,"objects":[{"label":"x","description":"y","confidence":7}',
])
def test_ac_per_26_nothing_salvageable_returns_none(text):
    assert salvage_observation(text) is None


def test_ac_per_26_complete_but_invalid_object_is_not_salvaged():
    bad = dict(VALID, unexpected="field")
    assert salvage_observation(compact(bad)) is None


def test_ac_per_26_length_envelope_is_salvaged_without_retry():
    cut = REAL_TEXT[: REAL_SPANS[3][0] + 30]
    rec = Recorder(envelope(cut, finish_reason="length"), envelope(compact(VALID)))
    res = budget_client(rec).analyze(window())
    assert len(rec.requests) == 1, "a salvaged reply is never retried"
    assert res.ok and res.status == "ok"
    assert res.truncated is True
    assert res.finish_reason == "length"
    assert res.attempts == 1
    assert res.observation["objects"] == REAL["objects"][:3]
    assert validate_observation(res.observation) == []
    assert res.raw == cut
    assert "truncated" not in res.observation  # sidecar, not in the schema-validated payload


def test_ac_per_26_unterminated_json_with_stop_is_also_salvaged():
    cut = REAL_TEXT[: REAL_SPANS[1][1] + 1]
    rec = Recorder(envelope(cut, finish_reason="stop"))
    res = budget_client(rec).analyze(window())
    assert len(rec.requests) == 1 and res.ok and res.truncated
    assert res.observation["objects"] == REAL["objects"][:2]


def test_ac_per_26_length_with_nothing_usable_keeps_the_invalid_path():
    rec = Recorder(envelope('{"scene_summary":"A person wor', finish_reason="length"),
                   envelope('{"scene_summary":"still cut', finish_reason="length"))
    res = budget_client(rec).analyze(window())
    assert len(rec.requests) == 2, "unsalvageable -> repair retry as before"
    assert res.status == "invalid" and res.observation is None and not res.truncated


def test_ac_per_26_salvage_on_the_repair_attempt():
    rec = Recorder(envelope("no json"), envelope(REAL_TEXT[: REAL_SPANS[0][1] + 1], finish_reason="length"))
    res = budget_client(rec).analyze(window())
    assert len(rec.requests) == 2 and res.ok and res.truncated and res.attempts == 2
    assert res.observation["objects"] == REAL["objects"][:1]


def test_ac_per_26_complete_reply_is_not_truncated():
    res = budget_client(Recorder(envelope(compact(REAL)))).analyze(window())
    assert res.ok and res.truncated is False and res.observation == REAL


def test_ac_per_26_fake_omni_salvages_scripted_cut_replies():
    res = FakeOmni([REAL_TEXT[: REAL_SPANS[2][1]]]).analyze(window())
    assert res.ok and res.truncated and len(res.observation["objects"]) == 3


# ---- adversarial: strings that look like structure, escapes, unicode
def _tricky() -> dict:
    return observation(
        [obj("laptop", [0.1, 0.1, 0.2, 0.2], description='a "{weird}" [label] }],{ \\ end'),
         obj("cargo transfer bag", [0.3, 0.3, 0.5, 0.5], description="Ünïcödé ✓ résumé 🚀 bag"),
         obj("drill", [0.5, 0.5, 0.7, 0.7], description='says: "}]}" then "\\"" more')],
        scene_summary='Crew {in} "the" lab ] }', location_hint="LAB1")


@pytest.mark.parametrize("ensure_ascii", [True, False])
def test_ac_per_26_salvage_ignores_braces_quotes_and_unicode_inside_strings(ensure_ascii):
    o = _tricky()
    text = json.dumps(o, separators=(",", ":"), ensure_ascii=ensure_ascii)
    raw = text.encode("utf-8")
    for offset in range(len(raw)):
        s = salvage_observation(raw[:offset].decode("utf-8", errors="ignore"))
        if s is None:
            continue
        assert validate_observation(s) == []
        assert s["scene_summary"] == o["scene_summary"] and s["location_hint"] == "LAB1"
        n = len(s["objects"])
        assert s["objects"] == o["objects"][:n], offset


def test_ac_per_26_cut_inside_a_unicode_escape_drops_that_element():
    o = _tricky()
    text = json.dumps(o, separators=(",", ":"), ensure_ascii=True)
    cut = text.index("\\u00dc") + 3  # inside Ü of the second object
    s = salvage_observation(text[:cut])
    assert s["objects"] == o["objects"][:1]


def test_ac_per_26_cut_moved_event_is_dropped_never_partial():
    o = observation([obj("drill", [0.1, 0.1, 0.3, 0.3])],
                    events=[{"type": "used", "object_label": "drill", "confidence": 0.8},
                            {"type": "moved", "object_label": "drill", "to_location_hint": "Node 2 crew quarters",
                             "confidence": 0.9}],
                    location_hint="LAB1")
    text = compact(o)
    start = text.index('{"type":"moved"')
    end = text.index("}", start) + 1
    for offset in range(start, end):  # every cut inside the moved event, including before its last brace
        s = salvage_observation(text[:offset])
        assert s["events"] == o["events"][:1], offset
    assert salvage_observation(text[: end + 1])["events"] == o["events"]


def test_ac_per_26_privacy_cut_inside_private_location_hint_is_not_salvaged():
    o = observation([obj("laptop", [0.1, 0.1, 0.3, 0.3])], location_hint="Node 2 crew quarters")
    text = compact(o)
    start = text.index('"location_hint"')
    closing_quote = text.index('"Node 2 crew quarters"') + len('"Node 2 crew quarters"') - 1
    for offset in range(start, closing_quote + 1):
        assert salvage_observation(text[:offset]) is None, offset


# ================================================================================ AC-PER-26 reconciler
def _truncated(objects, **kw) -> OmniResult:
    return OmniResult(status="ok", observation=observation(objects), model="m", raw="{}", truncated=True, **kw)


def test_ac_per_26_reconciler_caps_confidence_of_a_truncated_window(make_reconciler, inv, caplog):
    frames = [[("laptop-A", BOX_A)]]
    w = scene_window(frames)
    caplog.set_level(logging.INFO, logger="perception.reconciler")
    out = make_reconciler(omni_truncated_confidence=0.4).process(
        w, _truncated([obj("laptop", BOX_A, confidence=0.95)]), detections_for(frames, {"laptop-A": "laptop"}))
    assert out.status == "posted"
    (a,) = inv.last["assignments"]
    assert a["sighting"]["confidence"] == pytest.approx(0.4)
    assert any("truncated" in r.getMessage() for r in caplog.records)


def test_ac_per_26_reconciler_untruncated_window_keeps_confidence(make_reconciler, inv):
    process(make_reconciler(), [[("laptop-A", BOX_A)]], [obj("laptop", BOX_A, confidence=0.95)],
            {"laptop-A": "laptop"})
    (a,) = inv.last["assignments"]
    assert a["sighting"]["confidence"] == pytest.approx(0.95)


@pytest.mark.parametrize("flags", [{"truncated": True}, {"dropped_objects": 3}])
def test_ac_per_26_27_incomplete_window_writes_no_stock_updates(make_reconciler, inv, flags):
    counts = [3, 5]
    frames = food_frames(counts)
    labels = {f"food-{j}": "food package" for j in range(6)}
    omni = OmniResult(status="ok", observation=observation(food_objects(counts)), model="m", raw="{}", **flags)
    out = make_reconciler().process(scene_window(frames), omni, detections_for(frames, labels))
    assert out.status == "posted"
    assert inv.last["stock_updates"] == []


# ================================================================================ AC-PER-27 object cap
def test_ac_per_27_cap_keeps_highest_confidence_in_reply_order():
    objs = [obj(f"thing {i}", None, confidence=c) for i, c in enumerate([0.2, 0.9, 0.5, 0.8, 0.1])]
    kept, dropped = cap_objects(objs, 3)
    assert [o["label"] for o in kept] == ["thing 1", "thing 2", "thing 3"]
    assert dropped == 2


def test_ac_per_27_cap_is_noop_under_the_limit():
    objs = [obj("a"), obj("b")]
    assert cap_objects(objs, 12) == (objs, 0)


def test_ac_per_27_cap_never_drops_the_only_listing_of_an_unknown_for_a_repeat():
    objs = [obj("laptop", [0.1, 0.1, 0.2, 0.2], frame_index=i % 6, confidence=0.9) for i in range(14)]
    objs.insert(7, obj("mysterious gizmo", [0.6, 0.6, 0.7, 0.7], confidence=0.3))
    kept, dropped = cap_objects(objs, 12)
    assert len(kept) == 12 and dropped == 3
    assert [o["label"] for o in kept].count("mysterious gizmo") == 1


def test_ac_per_27_cap_label_diversity_is_case_and_space_insensitive():
    objs = [obj("Laptop ", confidence=0.9), obj("laptop", confidence=0.8), obj("drill", confidence=0.1)]
    kept, _ = cap_objects(objs, 2)
    assert [o["label"] for o in kept] == ["Laptop ", "drill"]


def test_ac_per_27_cap_tolerates_bad_confidence_values():
    objs = [{"label": "a", "confidence": "high"}, obj("b", confidence=0.5), "junk"]
    kept, dropped = cap_objects(objs, 1)
    assert dropped == 2 and kept == [objs[1]]


def test_ac_per_27_client_caps_objects_and_reports_dropped():
    o = observation([obj(f"item {i}", None, confidence=0.5 + i / 100) for i in range(20)])
    res = budget_client(Recorder(envelope(compact(o))), max_objects=12).analyze(window())
    assert res.ok and len(res.observation["objects"]) == 12 and res.dropped_objects == 8
    assert validate_observation(res.observation) == []


def test_ac_per_27_more_than_schema_max_is_capped_not_rejected():
    o = observation([obj(f"item {i}", None) for i in range(45)])
    assert validate_observation(o)  # 45 > maxItems 40
    rec = Recorder(envelope(compact(o)))
    res = budget_client(rec).analyze(window())
    assert len(rec.requests) == 1 and res.ok and len(res.observation["objects"]) == 12


def test_ac_per_27_interpret_reply_caps_salvaged_objects():
    r = interpret_reply(REAL_TEXT[: REAL_SPANS[10][1]], finish_reason="length", max_objects=4)
    assert r.observation is not None and r.truncated
    assert len(r.observation["objects"]) == 4 and r.dropped_objects == 7


# ================================================================================ AC-PER-28 usage logging
def test_ac_per_28_usage_logged_per_request(caplog):
    caplog.set_level(logging.INFO, logger="perception.omni")
    usage = {"prompt_tokens": 2200, "completion_tokens": 412, "total_tokens": 2612}
    w = window()
    res = budget_client(Recorder(envelope(compact(VALID), usage=usage))).analyze(w)
    recs = [r for r in caplog.records if r.getMessage() == "omni usage"]
    assert len(recs) == 1
    r = recs[0]
    assert (r.window, r.attempt, r.prompt_tokens, r.completion_tokens, r.finish_reason) == (
        w.index, 1, 2200, 412, "stop")
    assert isinstance(r.latency_ms, int) and r.truncated is False and r.dropped_objects == 0
    assert (res.prompt_tokens, res.completion_tokens, res.finish_reason) == (2200, 412, "stop")


def test_ac_per_28_usage_logged_for_each_attempt_and_summed(caplog):
    caplog.set_level(logging.INFO, logger="perception.omni")
    u1 = {"prompt_tokens": 2000, "completion_tokens": 700}
    u2 = {"prompt_tokens": 2800, "completion_tokens": 300}
    rec = Recorder(envelope('{"scene_summary":"cut', finish_reason="length", usage=u1),
                   envelope(compact(VALID), usage=u2))
    res = budget_client(rec).analyze(window())
    recs = [r for r in caplog.records if r.getMessage() == "omni usage"]
    assert [(r.attempt, r.finish_reason, r.completion_tokens) for r in recs] == [(1, "length", 700),
                                                                                 (2, "stop", 300)]
    assert (res.prompt_tokens, res.completion_tokens) == (4800, 1000)


def test_ac_per_28_usage_logged_with_truncation_flag(caplog):
    caplog.set_level(logging.INFO, logger="perception.omni")
    budget_client(Recorder(envelope(REAL_TEXT[:600], finish_reason="length",
                                    usage={"prompt_tokens": 1, "completion_tokens": 700}))).analyze(window())
    (r,) = [r for r in caplog.records if r.getMessage() == "omni usage"]
    assert r.truncated is True and r.finish_reason == "length"


def test_ac_per_28_missing_usage_is_logged_as_null(caplog):
    caplog.set_level(logging.INFO, logger="perception.omni")
    res = budget_client(Recorder(envelope(compact(VALID)))).analyze(window())
    (r,) = [r for r in caplog.records if r.getMessage() == "omni usage"]
    assert r.prompt_tokens is None and r.completion_tokens is None
    assert res.prompt_tokens is None and res.completion_tokens is None


# ================================================================================ adversarial review round
def test_review_salvage_skips_prose_that_looks_like_a_json_key():
    cut = REAL_TEXT[: REAL_SPANS[1][1] + 1]
    s = salvage_observation('Note: {"a" is how keys start}. ' + cut)
    assert s is not None and s["objects"] == REAL["objects"][:2]


def test_review_salvage_skips_a_complete_non_observation_object_before_the_reply():
    s = salvage_observation('{"note": "draft"} ' + REAL_TEXT[: REAL_SPANS[0][1] + 1])
    assert s is not None and s["objects"] == REAL["objects"][:1]


def test_review_cap_keeps_objects_referenced_by_events():
    """Dropping the only listing of an object an event names would make the reconciler drop a real movement."""
    objs = [obj(f"thing {i}", None, confidence=0.9) for i in range(12)]
    objs.append(obj("Drill", None, confidence=0.2))
    o = observation(objs, events=[{"type": "moved", "object_label": "drill", "to_location_hint": "NOD2",
                                   "confidence": 0.9}])
    r = interpret_reply(compact(o), max_objects=12)
    assert "Drill" in [x["label"] for x in r.observation["objects"]]
    assert r.dropped_objects == 1


def test_review_cap_keeps_held_objects():
    objs = [obj(f"thing {i}", None, confidence=0.9) for i in range(12)]
    objs.append(obj("wrench", None, confidence=0.2, held_by="P1"))
    kept, _ = cap_objects(objs, 12)
    assert "wrench" in [x["label"] for x in kept]


def test_review_cap_with_event_labels_argument():
    objs = [obj("a", confidence=0.9), obj("b", confidence=0.8), obj("c", confidence=0.1)]
    kept, dropped = omni_mod.cap_objects(objs, 2, keep_labels={"c"})
    assert [x["label"] for x in kept] == ["a", "c"] and dropped == 1


@pytest.mark.parametrize("hint", ["Node 2 crew quarters", "Node 3 WHC module"])
def test_review_salvaged_private_window_stores_nothing(make_reconciler, inv, storage, hint):
    """A cut-off reply whose (complete) hint names a no_capture place stays private end to end."""
    o = observation([obj("laptop", BOX_A), obj("glowing orb", (0.55, 0.5, 0.85, 0.8))], location_hint=hint,
                    transcript="talking in my quarters")
    cut = compact(o)
    cut = cut[: cut.index('"people"') - 1]  # objects complete, everything after cut off
    res = budget_client(Recorder(envelope(cut, finish_reason="length"))).analyze(window())
    assert res.ok and res.truncated and res.observation["location_hint"] == hint
    frames = [[("laptop-A", BOX_A), ("orb-1", (0.55, 0.5, 0.85, 0.8))]]
    make_reconciler(camera_id="cam-handheld").process(
        scene_window(frames), res, detections_for(frames, {"laptop-A": "laptop", "orb-1": "glowing orb"}),
        transcript="talking in my quarters")
    assert storage.objects == {}
    p = inv.last
    assert p.get("reviews", []) == [] and p["window"].get("raw_uri") is None and p["window"]["transcript"] is None  # AC-PER-43: null


def test_review_cap_keeps_every_distinct_instance_before_repeats():
    """Found by the eval's truncated profile: label diversity alone let 8 repeats of one laptop crowd out a second,
    physically different laptop (pairwise_f1 0.34). Distinct instances (label + box, AC-PER-19 IoU) come first."""
    a, b = [0.1, 0.1, 0.3, 0.3], [0.6, 0.6, 0.8, 0.8]
    others = [obj(f"tool {i}", [0.4, 0.05 * i, 0.45, 0.05 * i + 0.04]) for i in range(4)]
    objs = ([obj("laptop", a, frame_index=k % 6) for k in range(8)] + others
            + [obj("laptop", b, frame_index=k % 6) for k in range(8)])
    kept, dropped = cap_objects(objs, 12)
    assert dropped == 8
    assert {tuple(o["bbox"]) for o in kept if o["label"] == "laptop"} == {tuple(a), tuple(b)}
    assert all(o in kept for o in others)


def test_review_cap_overlapping_boxes_are_one_instance():
    a, a2, c = [0.1, 0.1, 0.3, 0.3], [0.11, 0.1, 0.31, 0.3], [0.6, 0.6, 0.8, 0.8]
    objs = [obj("laptop", a, confidence=0.9), obj("laptop", a2, confidence=0.8), obj("drill", c, confidence=0.1)]
    kept, _ = cap_objects(objs, 2)
    assert [o["label"] for o in kept] == ["laptop", "drill"]


def test_review_cap_boxless_repeats_of_a_label_are_one_instance():
    objs = [obj("laptop", None, frame_index=k, confidence=0.9) for k in range(3)] + [obj("drill", None,
                                                                                         confidence=0.2)]
    kept, _ = cap_objects(objs, 2)
    assert [o["label"] for o in kept] == ["laptop", "drill"]
