"""Regression tests for the reviewer's findings (fix round). Each test pins one finding; see docs/tdd-log/perception.md
"Review round 2". Grouped by acceptance criterion."""
from __future__ import annotations

import json
from datetime import timedelta

import httpx
import pytest
from support import (
    BUCKET,
    CAMERA_LOCATIONS,
    CATALOG,
    LOCATIONS,
    T0,
    FakeInventory,
    blend,
    obj,
    observation,
    random_unit,
    sine,
)
from test_ac_per_08_reconciler import BOX_A, BOX_B, process

import perception.replay
from perception import NEW
from perception.config import PerceptionConfig
from perception.decision import DecisionClient
from perception.fakes import FakeASR, FakeDecision, FakeDetector, FakeEmbedder, FakeOmni, MemoryStorage, make_frame
from perception.locations import LocationResolver
from perception.reconciler import Reconciler
from perception.speech import parse_speech_claim
from perception.types import ReplayStats

VEC = FakeEmbedder()


def vec(object_id):
    return VEC.vector_for_object(object_id)


# ================================================================================ AC-PER-11 privacy fails closed
def _cq_window(make_reconciler, *, camera_id, hint, transcript=""):
    frames = [[("laptop-A", BOX_A), ("orb-1", BOX_B)]]
    return process(make_reconciler(camera_id=camera_id), frames,
                   [obj("laptop", BOX_A), obj("glowing orb", BOX_B)], {"laptop-A": "laptop", "orb-1": "glowing orb"},
                   location_hint=hint, transcript=transcript)


@pytest.mark.parametrize("hint", ["Node 2", "NOD2", "LAB1", "Destiny", "Columbus"])
def test_ac_per_11_model_hint_cannot_switch_privacy_off_for_no_capture_camera(make_reconciler, inv, storage, config,
                                                                              hint):
    config.camera_locations["cam-cq"] = "NOD2P5"
    _cq_window(make_reconciler, camera_id="cam-cq", hint=hint)
    assert storage.objects == {}, list(storage.objects)
    p = inv.last
    assert p.get("reviews", []) == []
    assert p["window"].get("raw_uri") is None
    for a in p["assignments"]:
        assert a["sighting"].get("frame_uri") is None
        assert (a.get("exemplar") or {}).get("crop_uri") is None
        # an untrusted hint never moves a window out of the private camera's zone
        assert a["sighting"]["location_id"] == "NOD2P5"


def test_ac_per_11_no_capture_resolved_location_is_still_private(make_reconciler, inv, storage):
    """A capture-zone camera whose hint resolves into crew quarters is private (either side private -> private)."""
    _cq_window(make_reconciler, camera_id="cam-nod2-aft", hint="NOD2S5")
    assert storage.objects == {}
    assert inv.last.get("reviews", []) == []


def test_ac_per_11_unknown_camera_fails_closed(make_reconciler, inv, storage, config):
    assert "cam-mystery" not in config.camera_locations
    _cq_window(make_reconciler, camera_id="cam-mystery", hint=None)
    assert storage.objects == {}, list(storage.objects)
    assert inv.last.get("reviews", []) == []
    assert inv.last["window"].get("raw_uri") is None


def test_ac_per_11_unknown_camera_with_hint_still_fails_closed(make_reconciler, inv, storage, config):
    _cq_window(make_reconciler, camera_id="cam-mystery", hint="LAB1")
    assert storage.objects == {}


def test_ac_per_11_cli_refuses_camera_without_configured_location(monkeypatch, sample_video, capsys):
    import perception.__main__ as cli

    called = []
    monkeypatch.setattr(perception.replay, "run", lambda *a, **k: called.append(1) or ReplayStats())
    for k, v in {"INVENTORY_API_URL": "http://inventory-api:8080", "OMNI_URL": "http://omni:8000",
                 "DECISION_URL": "http://decision:8000"}.items():
        monkeypatch.setenv(k, v)
    monkeypatch.delenv("CAMERA_LOCATIONS", raising=False)
    rc = cli.main(["replay", "--source", str(sample_video), "--camera", "cam-mystery"])
    assert rc == 2
    assert called == []
    assert "cam-mystery" in capsys.readouterr().err


def test_ac_per_11_no_capture_window_stores_no_transcript_or_utterances(make_reconciler, inv, config):
    config.camera_locations["cam-cq"] = "NOD2P5"
    inv.add_entity(catalog_id="drill-pistol", embedding=vec("drill-1"), last_location="LAB1",
                   last_seen_at=T0 - timedelta(hours=1))
    _cq_window(make_reconciler, camera_id="cam-cq", hint=None, transcript="I'm putting the drill in node 2.")
    p = inv.last
    assert p["window"].get("transcript") in (None, "")
    assert "putting the drill" not in json.dumps(p["decisions"])


def test_ac_per_11_no_capture_window_sends_no_person_exemplar(make_reconciler, inv, config):
    config.camera_locations["cam-cq"] = "NOD2P5"
    process(make_reconciler(camera_id="cam-cq"), [[("crew-1", BOX_A)]], [obj("crew member", BOX_A)],
            {"crew-1": "crew member"})
    # AC-PER-36 (stricter): no person sighting at all in a private zone, so certainly no exemplar
    assert all(a.get("exemplar") is None for a in inv.last["assignments"])
    assert inv.last["assignments"] == []


# ================================================================================ AC-PER-10/16 speech claim parsing
@pytest.mark.parametrize("text", [
    "The drill came from node 2.",
    "The drill is not in node 2.",
    "I used the drill in the lab.",
    "I didn't put the drill in node 2.",
    "I never left the multimeter in Destiny.",
    "I'm taking the drill out of node 2.",
    "We don't keep the wrench in Columbus.",
])
def test_ac_per_16_parse_speech_claim_rejects_non_placement(text):
    assert parse_speech_claim(text, CATALOG, LocationResolver(LOCATIONS)) is None


@pytest.mark.parametrize("text,catalog_id,location_id", [
    ("I'm taking the drill from the lab to node 2.", "drill-pistol", "NOD2"),
    ("The drill is in node 2 now", "drill-pistol", "NOD2"),
    ("The drill's in node 2.", "drill-pistol", "NOD2"),
    ("Moved the wrench into Columbus.", "wrench", "COL1"),
    ("Put the drill in node 2.", "drill-pistol", "NOD2"),
])
def test_ac_per_16_parse_speech_claim_accepts_placement(text, catalog_id, location_id):
    claim = parse_speech_claim(text, CATALOG, LocationResolver(LOCATIONS))
    assert claim is not None
    assert (claim.catalog_id, claim.location_id) == (catalog_id, location_id)


@pytest.mark.parametrize("text", ["The drill came from node 2.", "I used the drill in the lab."])
def test_ac_per_16_non_placement_statement_makes_no_event_and_no_flag(make_reconciler, inv, text):
    eid = inv.add_entity(catalog_id="drill-pistol", embedding=vec("drill-1"), last_location="LAB1",
                         last_seen_at=T0 - timedelta(hours=1))
    process(make_reconciler(), [[]], [], {}, transcript=text)
    assert [e for e in inv.last.get("events", []) if e["source"] == "speech"] == []
    assert inv.entities[eid]["needs_review"] is False
    assert inv.patches == []


# ================================================================================ AC-PER-16 confirmation state
def _drill(inv, location="LAB1", **kw):
    return inv.add_entity(catalog_id="drill-pistol", embedding=vec("drill-1"), last_location=location,
                          last_seen_at=kw.pop("last_seen_at", T0 - timedelta(hours=1)), **kw)


def _reconciler(inv, storage, config, camera_id, **kw):
    return Reconciler(api=inv, embedder=FakeEmbedder(), decision=FakeDecision(), storage=storage, config=config,
                      camera_id=camera_id, catalog=CATALOG, locations=LOCATIONS, **kw)


def _sight_drill(rec, offset_s):
    return process(rec, [[("drill-1", BOX_A)]], [obj("drill", BOX_A)], {"drill-1": "drill"},
                   t0=T0 + timedelta(seconds=offset_s))


def test_ac_per_16_claim_heard_by_one_camera_is_confirmed_by_another(inv, storage, config):
    eid = _drill(inv)
    heard_on = _reconciler(inv, storage, config, "cam-lab1-fwd")
    process(heard_on, [[]], [], {}, transcript="I'm putting the drill in node 2.")
    assert inv.entities[eid]["needs_review"] is True
    seen_on = _reconciler(inv, storage, config, "cam-nod2-aft", scorer=lambda f: 0.95)  # another process
    _sight_drill(seen_on, 300)
    assert str(inv.last["assignments"][0]["entity_id"]) == eid
    assert inv.entities[eid]["needs_review"] is False
    clear = [f for e, f in inv.patches if e == eid and f.get("needs_review") is False]
    assert len(clear) == 1 and clear[0].get("note")


def test_ac_per_16_claim_survives_restart(inv, storage, config):
    eid = _drill(inv)
    process(_reconciler(inv, storage, config, "cam-nod2-aft"), [[]], [], {},
            transcript="I'm putting the drill in node 2.")
    restarted = _reconciler(inv, storage, config, "cam-nod2-aft", scorer=lambda f: 0.95)
    _sight_drill(restarted, 120)
    assert inv.entities[eid]["needs_review"] is False


def test_ac_per_16_confirming_sighting_does_not_clear_flag_of_open_review(inv, storage, config):
    eid = _drill(inv)
    process(_reconciler(inv, storage, config, "cam-lab1-fwd"), [[]], [], {},
            transcript="I'm putting the drill in node 2.")
    # meanwhile inventory-api opened a possible_duplicate review on the same entity
    inv.entities[eid]["needs_review"] = True
    inv.entities[eid]["review_reason"] = "possible_duplicate"
    _sight_drill(_reconciler(inv, storage, config, "cam-nod2-aft", scorer=lambda f: 0.95), 200)
    assert inv.entities[eid]["needs_review"] is True
    assert [f for e, f in inv.patches if e == eid and f.get("needs_review") is False] == []


def test_ac_per_16_expired_claim_becomes_a_reviewable_conflict(inv, storage, config):
    eid = _drill(inv)
    process(_reconciler(inv, storage, config, "cam-lab1-fwd"), [[]], [], {},
            transcript="I'm putting the drill in node 2.")
    rec = _reconciler(inv, storage, config, "cam-lab1-fwd", scorer=lambda f: 0.95)
    _sight_drill(rec, 11 * 60)
    conflicts = [r for r in inv.reviews if r["kind"] == "conflict"]
    assert len(conflicts) == 1
    assert conflicts[0]["resolved_entity"] == eid
    assert conflicts[0]["image_uri"].startswith(f"s3://{BUCKET}/")
    assert "NOD2" in conflicts[0]["description"]
    assert inv.entities[eid]["needs_review"] is True
    _sight_drill(rec, 12 * 60)
    assert len([r for r in inv.reviews if r["kind"] == "conflict"]) == 1, "one conflict review per claim"


def test_ac_per_16_speech_claim_prefers_entity_near_the_camera(make_reconciler, inv):
    lab_drill = _drill(inv, "LAB1", last_seen_at=T0 - timedelta(hours=2))
    _drill(inv, "COL1", last_seen_at=T0 - timedelta(minutes=5))  # more recent, but in another module
    process(make_reconciler(camera_id="cam-lab1-fwd"), [[]], [], {}, transcript="I'm putting the drill in node 2.")
    speech = [e for e in inv.last["events"] if e["source"] == "speech"]
    assert len(speech) == 1 and str(speech[0]["entity_id"]) == lab_drill


# ================================================================================ AC-PER-8 step 6 unknown dedup
def test_ac_per_08_unknown_resighting_not_crowded_out_by_classified_lookalikes(make_reconciler, inv):
    orb = vec("orb-1")
    unknown = inv.add_entity(catalog_id=None, label="glowing orb", status="unknown",
                             embedding=blend(orb, random_unit(1), 0.92), last_location="LAB1",
                             last_seen_at=T0 - timedelta(minutes=10))
    for i in range(12):  # catalogued look-alikes that rank above the unknown entity
        inv.add_entity(catalog_id="flashlight", embedding=blend(orb, random_unit(100 + i), 0.95),
                       last_location="COL1", last_seen_at=T0 - timedelta(days=2))
    process(make_reconciler(), [[("orb-1", BOX_A)]], [obj("glowing orb", BOX_A)], {"orb-1": "glowing orb"})
    a = inv.last["assignments"][0]
    assert str(a.get("entity_id")) == unknown
    assert [r for r in inv.reviews if r["kind"] == "unknown_object"] == []


def test_ac_per_08_unknown_label_can_match_entity_classified_after_review(make_reconciler, inv):
    """NASA resolved the unknown orb with assign_catalog; the omni model still calls it 'glowing orb'."""
    eid = inv.add_entity(catalog_id="flashlight", label="flashlight", status="confirmed", embedding=vec("orb-1"),
                         last_location="LAB1", last_seen_at=T0 - timedelta(minutes=10))
    dec = FakeDecision(lambda q, o, s: ({"choice": eid, "probabilities": {eid: 0.9, NEW: 0.1}}
                                        if q == "same_or_new" else TimeoutError()))
    process(make_reconciler(decision=dec), [[("orb-1", BOX_A)]], [obj("glowing orb", BOX_A)],
            {"orb-1": "glowing orb"})
    assert str(inv.last["assignments"][0].get("entity_id")) == eid
    assert [r for r in inv.reviews if r["kind"] == "unknown_object"] == []


def test_ac_per_08_catalog_is_refreshed_after_ttl(storage, config):
    inv = FakeInventory()
    now = [0.0]
    rec = Reconciler(api=inv, embedder=FakeEmbedder(), decision=FakeDecision(), storage=storage, config=config,
                     camera_id="cam-lab1-fwd", clock=lambda: now[0])
    inv.catalog.append({"catalog_id": "glow-orb", "class_name": "glowing orb", "display_name": "Glowing orb",
                        "synonyms": [], "category": "object", "is_fungible": False, "expected_count": None,
                        "home_location": None})
    process(rec, [[("orb-1", BOX_A)]], [obj("glowing orb", BOX_A)], {"orb-1": "glowing orb"})
    assert inv.last["assignments"][0]["new_entity"]["catalog_id"] is None  # catalog not reloaded yet
    now[0] = config.catalog_refresh_s + 1
    process(rec, [[("orb-2", BOX_A)]], [obj("glowing orb", BOX_A)], {"orb-2": "glowing orb"},
            t0=T0 + timedelta(minutes=10))
    assert inv.last["assignments"][0]["new_entity"]["catalog_id"] == "glow-orb"


def test_ac_per_18_replay_refreshes_detector_labels_with_catalog(storage):
    inv = FakeInventory()
    now = [0.0]
    detector = FakeDetector({"laptop-A": "laptop"})
    config = PerceptionConfig(camera_locations=dict(CAMERA_LOCATIONS), s3_bucket=BUCKET, window_frames=1,
                              stride_s=1.0)

    class Source:
        def __iter__(self):
            yield (T0, make_frame([("laptop-A", BOX_A)]), None)
            inv.catalog.append({"catalog_id": "glow-orb", "class_name": "glowing orb", "display_name": "Glowing orb",
                                "synonyms": [], "category": "object", "is_fungible": False})
            now[0] = config.catalog_refresh_s + 1
            yield (T0 + timedelta(seconds=5), make_frame([("laptop-A", BOX_A)]), None)

    perception.replay.run(Source(), camera_id="cam-lab1-fwd", start_time=T0, api=inv, omni=FakeOmni([]),
                          detector=detector, embedder=FakeEmbedder(), decision=FakeDecision(), asr=None,
                          storage=storage, config=config, clock=lambda: now[0])
    assert "glowing orb" not in detector.calls[0][1]
    assert "glowing orb" in detector.calls[-1][1]


# ================================================================================ AC-PER-10 event dedup
MOVED = {"type": "moved", "object_label": "drill", "to_location_hint": "NOD2", "confidence": 0.8}


def test_ac_per_10_same_event_in_overlapping_windows_is_recorded_once(make_reconciler, inv):
    _drill(inv)
    rec = make_reconciler(camera_id="cam-nod2-aft", scorer=lambda f: 0.95)
    frames = [[("drill-1", BOX_A)]] * 6
    process(rec, frames, [obj("drill", BOX_A, frame_index=5)], {"drill-1": "drill"},
            events=[{**MOVED, "frame_index": 5}])
    process(rec, frames, [obj("drill", BOX_A, frame_index=1)], {"drill-1": "drill"},
            events=[{**MOVED, "frame_index": 1}], t0=T0 + timedelta(seconds=4))
    assert len([e for e in inv.events if e["event_type"] == "moved"]) == 1
    # a genuinely later move of the same object is still recorded
    process(rec, frames, [obj("drill", BOX_A, frame_index=2)], {"drill-1": "drill"},
            events=[{**MOVED, "frame_index": 2}], t0=T0 + timedelta(minutes=5))
    assert len([e for e in inv.events if e["event_type"] == "moved"]) == 2


def test_ac_per_14_replay_default_windowing_dedups_repeated_omni_event():
    """Default windowing (6 frames, stride 4 s): consecutive windows share frames and the omni model reports the same
    movement in each of them."""
    inv = FakeInventory()
    items = [(T0 + timedelta(seconds=i), make_frame([("drill-1", BOX_A)]), None) for i in range(10)]
    obs = observation([obj("drill", BOX_A, frame_index=0)], events=[{**MOVED, "frame_index": 0}])
    later = observation([obj("drill", BOX_A, frame_index=0)], events=[{**MOVED, "frame_index": 0}])
    stats = perception.replay.run(
        items, camera_id="cam-nod2-aft", start_time=T0, api=inv, omni=FakeOmni([obs, later, later, later]),
        detector=FakeDetector({"drill-1": "drill"}), embedder=FakeEmbedder(), decision=FakeDecision(), asr=None,
        storage=MemoryStorage(BUCKET), config=PerceptionConfig(camera_locations=dict(CAMERA_LOCATIONS),
                                                               s3_bucket=BUCKET))
    assert stats.posted >= 3
    assert len([e for e in inv.events if e["event_type"] == "moved"]) == 1


# ================================================================================ AC-PER-12 decision client
def _client(handler, **kw):
    seen = []

    def wrapped(request):
        seen.append(request)
        return handler(request)

    client = DecisionClient("http://decision.test", model="dg",
                            http_client=httpx.Client(transport=httpx.MockTransport(wrapped)), **kw)
    return client, seen


@pytest.mark.parametrize("body", [{"choices": []}, {"choices": ["oops"]}, {"choices": [{"message": "x"}]},
                                  {"choices": [{"logprobs": {"content": ["x"]}}]}, ["not", "a", "dict"]])
def test_ac_per_12_malformed_chat_reply_never_raises(body):
    client, _ = _client(lambda r: httpx.Response(200, json=body), mode="chat")
    result = client.choose("same_or_new", {"e1": "a", NEW: "b"})
    assert result.choice == NEW and result.probabilities == {} and result.needs_review is True


def test_ac_per_12_missing_systemone_is_remembered():
    def handler(request):
        if request.url.path == "/v1/systemone":
            return httpx.Response(404)
        return httpx.Response(200, json={"choices": [{"message": {"content": "A"}}], "model": "dg"})

    client, seen = _client(handler)
    for _ in range(3):
        assert client.choose("same_or_new", {"e1": "a", NEW: "b"}).choice == "e1"
    assert [r.url.path for r in seen].count("/v1/systemone") == 1


# ================================================================================ AC-PER-15 ASR independent of omni
def test_ac_per_15_speech_in_window_with_failed_omni_is_not_lost():
    inv = FakeInventory()
    eid = inv.add_entity(catalog_id="drill-pistol", embedding=vec("drill-1"), last_location="LAB1",
                         last_seen_at=T0 - timedelta(hours=1))
    items = [(T0, make_frame([("drill-1", BOX_A)]), sine(5.0)),
             (T0 + timedelta(seconds=5), make_frame([("drill-1", BOX_A)]), sine(5.0))]
    asr = FakeASR(["I'm putting the drill in node 2.", "all good here"])
    omni = FakeOmni([None, observation([])])
    stats = perception.replay.run(
        items, camera_id="cam-lab1-fwd", start_time=T0, api=inv, omni=omni,
        detector=FakeDetector({"drill-1": "drill"}), embedder=FakeEmbedder(), decision=FakeDecision(), asr=asr,
        storage=MemoryStorage(BUCKET),
        config=PerceptionConfig(camera_locations=dict(CAMERA_LOCATIONS), s3_bucket=BUCKET, window_frames=1,
                                stride_s=1.0))
    assert stats.invalid == 1 and stats.posted == 1
    transcript = inv.last["window"]["transcript"]
    assert "putting the drill in node 2" in transcript and "all good here" in transcript
    speech = [e for e in inv.events if e["source"] == "speech"]
    assert len(speech) == 1 and speech[0]["resolved_entity"] == eid


# ================================================================================ secrets in logs
def test_redact_source_strips_credentials_and_query():
    from perception.ingest import redact_source

    assert redact_source("rtsp://user:pa55@cam.iss:554/live?token=abc") == "rtsp://cam.iss:554/live"
    assert redact_source("https://s3.local/b/v.mp4?X-Amz-Signature=deadbeef") == "https://s3.local/b/v.mp4"
    assert redact_source("/data/video.mp4") == "/data/video.mp4"


def test_ingest_error_does_not_leak_credentials():
    from perception.ingest import IngestError, iter_keyframes

    with pytest.raises(IngestError) as exc:
        next(iter_keyframes("rtsp://user:pa55@127.0.0.1:1/live?token=abc", start_time=T0))
    assert "pa55" not in str(exc.value) and "token=abc" not in str(exc.value)


# ================================================================================ AC-PER-16 against the real API
def test_ac_per_16_cross_camera_confirmation_through_real_inventory_api(api_app_client, db, storage, config):
    """The claim state round-trips through inventory-api (PATCH flag, GET /items?needs_review, GET /events)."""
    from perception.inventory_client import HttpInventoryClient

    api = HttpInventoryClient("http://testserver", http_client=api_app_client)

    def rec(camera_id):
        return Reconciler(api=api, embedder=FakeEmbedder(), decision=FakeDecision(), storage=storage, config=config,
                          camera_id=camera_id, scorer=lambda f: 0.95)

    process(rec("cam-lab1-fwd"), [[("drill-1", BOX_A)]], [obj("drill", BOX_A)], {"drill-1": "drill"})
    (eid,) = db.execute("SELECT entity_id FROM entity WHERE catalog_id = 'drill-pistol'").fetchone()
    process(rec("cam-lab1-fwd"), [[]], [], {}, transcript="I'm putting the drill in node 2.",
            t0=T0 + timedelta(seconds=30))
    assert db.execute("SELECT needs_review FROM entity WHERE entity_id = %s", (eid,)).fetchone()[0] is True
    outcome, _ = _sight_drill(rec("cam-nod2-aft"), 300)
    assert outcome.status == "posted"
    row = db.execute("SELECT needs_review, last_location FROM entity WHERE entity_id = %s", (eid,)).fetchone()
    assert row == (False, "NOD2")
