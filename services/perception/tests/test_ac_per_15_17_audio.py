"""AC-PER-15 ASR path (dedicated /v1/audio/transcriptions with a catalog/module context prompt; ASR transcript is the
stored record, omni transcript only when ASR is unavailable).
AC-PER-16 speech claims (statement/question/other via decision model with rule fallback; only statements produce
speech events; confidence <= 0.6; needs_review until a visual sighting in the claimed location within 10 min).
AC-PER-17 silence handling (RMS below VAD threshold skips ASR; empty/hallucinated transcripts discarded)."""
from __future__ import annotations

import io
import wave
from datetime import timedelta

import httpx
import numpy as np
import pytest
from support import CATALOG, LOCATIONS, T0, obj, observation, silence, sine
from test_ac_per_08_reconciler import BOX_A, process

from perception.asr import AsrClient, build_asr_context
from perception.fakes import FakeDecision, FakeEmbedder
from perception.locations import LocationResolver
from perception.speech import (
    classify_utterance,
    classify_utterance_rules,
    clean_transcript,
    is_hallucination,
    is_silent,
    parse_speech_claim,
    split_utterances,
)
from perception.types import AudioChunk, DecisionResult

VEC = FakeEmbedder()


# ------------------------------------------------------------------------------------------------ AC-PER-15 client
def _multipart_fields(request: httpx.Request) -> dict[str, bytes]:
    ctype = request.headers["content-type"]
    boundary = ctype.split("boundary=")[1].encode()
    fields = {}
    for part in request.content.split(b"--" + boundary):
        if b"Content-Disposition" not in part:
            continue
        head, _, body = part.partition(b"\r\n\r\n")
        name = head.split(b'name="')[1].split(b'"')[0].decode()
        fields[name] = body.rstrip(b"\r\n")
    return fields


def asr_client(handler) -> tuple[AsrClient, list]:
    seen = []

    def wrapped(request):
        seen.append(request)
        r = handler(request)
        if isinstance(r, Exception):
            raise r
        return r

    return AsrClient("http://asr.test", model="Qwen/Qwen3-ASR-1.7B",
                     http_client=httpx.Client(transport=httpx.MockTransport(wrapped))), seen


def test_ac_per_15_posts_wav_to_audio_transcriptions_with_context_prompt():
    client, seen = asr_client(lambda r: httpx.Response(200, json={"text": "putting the PGT in Node 2"}))
    chunk = AudioChunk(timestamp=T0, samples=sine(2.0))
    res = client.transcribe(chunk, context="drill, PGT, NOD2, Harmony")
    assert res.ok and res.text == "putting the PGT in Node 2"
    req = seen[0]
    assert req.method == "POST" and req.url.path == "/v1/audio/transcriptions"
    fields = _multipart_fields(req)
    assert fields["model"] == b"Qwen/Qwen3-ASR-1.7B"
    assert b"PGT" in fields["prompt"] and b"NOD2" in fields["prompt"]
    with wave.open(io.BytesIO(fields["file"])) as wf:
        assert (wf.getnchannels(), wf.getframerate(), wf.getsampwidth()) == (1, 16000, 2)
        assert wf.getnframes() == len(chunk.samples)


@pytest.mark.parametrize("response", [httpx.Response(500, text="boom"), httpx.Response(200, text="not json"),
                                      httpx.ConnectError("refused"), httpx.ReadTimeout("slow")])
def test_ac_per_15_failures_return_not_ok(response):
    client, _ = asr_client(lambda r: response)
    res = client.transcribe(AudioChunk(timestamp=T0, samples=sine(1.0)), context="")
    assert res.ok is False and res.text == ""


def test_ac_per_15_context_contains_catalog_names_and_module_codes():
    ctx = build_asr_context(CATALOG, LOCATIONS)
    for term in ("drill", "PGT", "cargo transfer bag", "CTB", "multimeter", "astrobee", "NOD2", "LAB1", "COL1",
                 "ZVEZDA", "Harmony", "Destiny"):
        assert term.lower() in ctx.lower(), term
    assert len(ctx) <= 2000


def test_ac_per_15_context_is_bounded():
    assert len(build_asr_context(CATALOG, LOCATIONS, max_chars=200)) <= 200


# ------------------------------------------------------------------------------------------------ AC-PER-15 pipeline
def _replay(source_items, *, omni, asr, inv, storage, decision=None, config=None):
    from support import BUCKET, CAMERA_LOCATIONS

    from perception.config import PerceptionConfig
    from perception.fakes import FakeDetector
    from perception.replay import run

    cfg = config or PerceptionConfig(camera_locations=dict(CAMERA_LOCATIONS), s3_bucket=BUCKET)
    return run(source_items, camera_id="cam-lab1-fwd", start_time=T0, api=inv, omni=omni,
               detector=FakeDetector({"drill-1": "drill"}), embedder=FakeEmbedder(),
               decision=decision or FakeDecision(),
               asr=asr, storage=storage, config=cfg)


def _one_window_source(audio):
    from perception.fakes import make_frame

    return [(T0, make_frame([("drill-1", BOX_A)]), audio)]


def test_ac_per_15_asr_transcript_is_the_stored_record(inv, storage):
    from perception.fakes import FakeASR, FakeOmni

    asr = FakeASR(["the drill is on the rack"])
    omni = FakeOmni([observation([obj("drill", BOX_A)], transcript="omni heard something else")])
    _replay(_one_window_source(sine(5.0)), omni=omni, asr=asr, inv=inv, storage=storage)
    assert len(asr.calls) == 1
    assert inv.last["window"]["transcript"] == "the drill is on the rack"
    chunk, context = asr.calls[0]
    assert chunk.sample_rate == 16000 and len(chunk.samples) == 80000
    assert "drill" in context and "NOD2" in context


def test_ac_per_15_omni_transcript_used_when_asr_unavailable(inv, storage):
    from perception.fakes import FakeASR, FakeOmni

    omni = FakeOmni([observation([obj("drill", BOX_A)], transcript="omni transcript fallback")])
    _replay(_one_window_source(sine(5.0)), omni=omni, asr=FakeASR(available=False), inv=inv, storage=storage)
    assert inv.last["window"]["transcript"] == "omni transcript fallback"


def test_ac_per_15_omni_transcript_used_when_no_asr_configured(inv, storage):
    from perception.fakes import FakeOmni

    omni = FakeOmni([observation([obj("drill", BOX_A)], transcript="omni only")])
    _replay(_one_window_source(sine(5.0)), omni=omni, asr=None, inv=inv, storage=storage)
    assert inv.last["window"]["transcript"] == "omni only"


def test_ac_per_15_empty_asr_result_is_not_replaced_by_omni(inv, storage):
    """ASR available but heard nothing: the ASR record ('') stands; the omni transcript is only a fallback for an
    unavailable ASR."""
    from perception.fakes import FakeASR, FakeOmni

    omni = FakeOmni([observation([], transcript="omni imagined words")])
    _replay(_one_window_source(sine(5.0)), omni=omni, asr=FakeASR([""]), inv=inv, storage=storage)
    assert inv.last["window"].get("transcript") in (None, "")


# ------------------------------------------------------------------------------------------------ AC-PER-17
def test_ac_per_17_is_silent():
    assert is_silent(AudioChunk(timestamp=T0, samples=silence(1.0)), threshold=300) is True
    assert is_silent(AudioChunk(timestamp=T0, samples=sine(1.0, amp=50)), threshold=300) is True
    assert is_silent(AudioChunk(timestamp=T0, samples=sine(1.0, amp=8000)), threshold=300) is False
    assert is_silent(None, threshold=300) is True
    assert is_silent(AudioChunk(timestamp=T0, samples=np.zeros(0, dtype=np.int16)), threshold=300) is True


@pytest.mark.parametrize("text,expected", [
    ("thank you thank you thank you thank you", True),
    ("Thank you. Thank you. Thank you. Thank you.", True),
    ("put the drill in node 2 put the drill in node 2 put the drill in node 2 put the drill in node 2", True),
    ("thank you thank you thank you", False),
    ("I'm putting the drill in node 2", False),
    ("the drill, the drill", False),
    ("", False),
])
def test_ac_per_17_hallucination_detection(text, expected):
    assert is_hallucination(text, max_repeats=3) is expected


def test_ac_per_17_clean_transcript():
    assert clean_transcript("  the drill is here ", silent=False) == "the drill is here"
    assert clean_transcript("the drill is here", silent=True) == ""
    assert clean_transcript("", silent=False) == ""
    assert clean_transcript(None, silent=False) == ""
    assert clean_transcript("bye bye bye bye bye", silent=False) == ""


def test_ac_per_17_silent_window_skips_asr_and_discards_text(inv, storage):
    from perception.fakes import FakeASR, FakeOmni

    asr = FakeASR(["you you you"])
    omni = FakeOmni([observation([obj("drill", BOX_A)], transcript="Thanks for watching!")])
    _replay(_one_window_source(silence(5.0)), omni=omni, asr=asr, inv=inv, storage=storage)
    assert asr.calls == []
    assert inv.last["window"].get("transcript") in (None, "")


def test_ac_per_17_window_without_audio_skips_asr(inv, storage):
    from perception.fakes import FakeASR, FakeOmni

    asr = FakeASR(["anything"])
    _replay(_one_window_source(None), omni=FakeOmni([observation([])]), asr=asr, inv=inv, storage=storage)
    assert asr.calls == []


def test_ac_per_17_hallucinated_asr_is_discarded_and_makes_no_event(inv, storage):
    from perception.fakes import FakeASR, FakeOmni

    inv.add_entity(catalog_id="drill-pistol", embedding=VEC.vector_for_object("drill-9"), last_location="LAB1",
                   last_seen_at=T0 - timedelta(hours=1))
    text = " ".join(["I put the drill in node 2."] * 5)
    _replay(_one_window_source(sine(5.0)), omni=FakeOmni([observation([])]), asr=FakeASR([text]), inv=inv,
            storage=storage)
    assert "drill" not in (inv.last["window"].get("transcript") or "")
    assert [e for e in inv.last.get("events", []) if e["source"] == "speech"] == []


def test_ac_per_17_vad_threshold_configurable(inv, storage):
    from support import BUCKET, CAMERA_LOCATIONS

    from perception.config import PerceptionConfig
    from perception.fakes import FakeASR, FakeOmni

    asr = FakeASR(["quiet words"])
    cfg = PerceptionConfig(camera_locations=dict(CAMERA_LOCATIONS), s3_bucket=BUCKET, vad_rms=10.0)
    _replay(_one_window_source(sine(5.0, amp=100)), omni=FakeOmni([observation([])]), asr=asr, inv=inv,
            storage=storage, config=cfg)
    assert len(asr.calls) == 1


# ------------------------------------------------------------------------------------------------ AC-PER-16 rules
@pytest.mark.parametrize("text,expected", [
    ("I'm putting the drill in node 2.", "statement"),
    ("The drill is in node 2 now", "statement"),
    ("I stowed the wrench in the CTB.", "statement"),
    ("Should I put the drill in node 2?", "question"),
    ("should I put the drill in node 2", "question"),
    ("Where is the drill", "question"),
    ("Is the drill in node 2", "question"),
    ("Can you hand me the multimeter", "question"),
    ("the drill is in node 2, right?", "question"),
    ("", "other"),
    ("   ", "other"),
])
def test_ac_per_16_rule_fallback_classification(text, expected):
    assert classify_utterance_rules(text) == expected


def test_ac_per_16_decision_model_classification_wins():
    dec = FakeDecision(lambda q, o, s: DecisionResult(choice="question", probabilities={"question": 0.9},
                                                      model="fake-decision"))
    assert classify_utterance("I'm putting the drill in node 2.", dec) == "question"
    _q, options, _ = dec.calls[0]
    keys = list(options.keys()) if isinstance(options, dict) else list(options)
    assert set(keys) == {"statement", "question", "other"}


def test_ac_per_16_decision_failure_uses_rule_fallback():
    dec = FakeDecision(lambda q, o, s: TimeoutError("down"))
    assert classify_utterance("Should I put the drill in node 2?", dec) == "question"
    assert classify_utterance("I'm putting the drill in node 2.", dec) == "statement"


def test_ac_per_16_no_decision_model_uses_rules():
    assert classify_utterance("Where is the drill?", None) == "question"


def test_ac_per_16_split_utterances():
    assert split_utterances("Put the drill in node 2. Where is the wrench? OK") == [
        "Put the drill in node 2.", "Where is the wrench?", "OK"]
    assert split_utterances("") == []


@pytest.mark.parametrize("text,catalog_id,location_id", [
    ("I'm putting the drill in node 2", "drill-pistol", "NOD2"),
    ("putting the PGT in Harmony", "drill-pistol", "NOD2"),
    ("I left the multimeter in Destiny", "multimeter", "LAB1"),
    ("stowing the wrench in Columbus", "wrench", "COL1"),
])
def test_ac_per_16_parse_speech_claim(text, catalog_id, location_id):
    claim = parse_speech_claim(text, CATALOG, LocationResolver(LOCATIONS))
    assert claim is not None
    assert (claim.catalog_id, claim.location_id) == (catalog_id, location_id)


@pytest.mark.parametrize("text", ["the drill is nice", "putting the banana in node 2", "", "node 2 is busy"])
def test_ac_per_16_parse_speech_claim_needs_item_and_location(text):
    assert parse_speech_claim(text, CATALOG, LocationResolver(LOCATIONS)) is None


# ------------------------------------------------------------------------------------------------ AC-PER-16 reconciler
def _drill(inv, location="LAB1"):
    return inv.add_entity(catalog_id="drill-pistol", embedding=VEC.vector_for_object("drill-1"),
                          last_location=location, last_seen_at=T0 - timedelta(hours=1))


def test_ac_per_16_question_produces_no_speech_event(make_reconciler, inv):
    _drill(inv)
    process(make_reconciler(), [[]], [], {}, transcript="Should I put the drill in node 2?")
    assert [e for e in inv.last.get("events", []) if e["source"] == "speech"] == []


def test_ac_per_16_decision_model_question_label_suppresses_event(make_reconciler, inv):
    _drill(inv)

    def answer(q, options, state):
        if q == "speech_act":
            return {"choice": "question", "probabilities": {"question": 0.8, "statement": 0.2}}
        return TimeoutError()

    process(make_reconciler(decision=FakeDecision(answer)), [[]], [], {},
            transcript="I'm putting the drill in node 2.")
    assert [e for e in inv.last.get("events", []) if e["source"] == "speech"] == []


def test_ac_per_16_speech_event_confidence_capped(make_reconciler, inv):
    _drill(inv)

    def answer(q, options, state):
        return {"choice": "statement", "probabilities": {"statement": 0.99}} if q == "speech_act" else TimeoutError()

    process(make_reconciler(decision=FakeDecision(answer)), [[]], [], {},
            transcript="I'm putting the drill in node 2.")
    speech = [e for e in inv.last["events"] if e["source"] == "speech"]
    assert len(speech) == 1 and 0.0 < speech[0]["confidence"] <= 0.6


def test_ac_per_16_speech_only_event_flags_entity_for_review(make_reconciler, inv):
    eid = _drill(inv)
    process(make_reconciler(), [[]], [], {}, transcript="I'm putting the drill in node 2.")
    assert inv.entities[eid]["needs_review"] is True


def _confirm_window(rec, offset_s):
    """A later window in which the camera sees the drill."""
    return process(rec, [[("drill-1", BOX_A)]], [obj("drill", BOX_A)], {"drill-1": "drill"},
                   t0=T0 + timedelta(seconds=offset_s))


def test_ac_per_16_visual_sighting_in_claimed_location_within_10_min_clears_flag(make_reconciler, inv):
    eid = _drill(inv)
    rec = make_reconciler(camera_id="cam-nod2-aft", scorer=lambda f: 0.95)
    process(rec, [[]], [], {}, transcript="I'm putting the drill in node 2.")
    assert inv.entities[eid]["needs_review"] is True
    _confirm_window(rec, 300)
    assert str(inv.last["assignments"][0]["entity_id"]) == eid
    assert inv.entities[eid]["needs_review"] is False
    assert (eid, {"needs_review": False}) in [(e, {k: v for k, v in f.items() if k == "needs_review"})
                                              for e, f in inv.patches]


def test_ac_per_16_sighting_after_10_min_does_not_clear_flag(make_reconciler, inv):
    eid = _drill(inv)
    rec = make_reconciler(camera_id="cam-nod2-aft", scorer=lambda f: 0.95)
    process(rec, [[]], [], {}, transcript="I'm putting the drill in node 2.")
    _confirm_window(rec, 11 * 60)
    assert str(inv.last["assignments"][0]["entity_id"]) == eid
    assert inv.entities[eid]["needs_review"] is True


def test_ac_per_16_sighting_in_other_location_does_not_clear_flag(make_reconciler, inv):
    eid = _drill(inv)
    rec = make_reconciler(camera_id="cam-lab1-fwd", scorer=lambda f: 0.95)
    process(rec, [[]], [], {}, transcript="I'm putting the drill in node 2.")
    _confirm_window(rec, 120)
    assert str(inv.last["assignments"][0]["entity_id"]) == eid
    assert inv.entities[eid]["needs_review"] is True


def test_ac_per_16_only_statements_among_mixed_utterances(make_reconciler, inv):
    _drill(inv)
    process(make_reconciler(), [[]], [], {},
            transcript="Should I put the drill in node 2? Yes. I'm putting the drill in node 2.")
    speech = [e for e in inv.last["events"] if e["source"] == "speech"]
    assert len(speech) == 1
