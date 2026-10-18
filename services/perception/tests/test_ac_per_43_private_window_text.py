"""AC-PER-43 private windows carry no text (UI review round 3, finding UI3-02).

A no_capture window (private zone, sleep cue, privacy hold) already stored no media; its scene summary was still sent
and became full-text searchable. Now the payload marks the window ``private`` with a coarse ``privacy_reason`` and
carries no scene summary, transcript, audio event labels, sighting or new-entity descriptions, or cue word."""
from __future__ import annotations

import json
from datetime import timedelta

import pytest
from support import T0, detections_for, obj, observation, scene_window

from perception.types import OmniResult

HARMONY_CAM = "cam-nod2-aft"
LAB_CAM = "cam-lab1-fwd"
BOX_L = (0.42, 0.60, 0.52, 0.75)
BOX_A = (0.05, 0.10, 0.40, 0.90)
MARK = "zebramark"  # a word that only appears in model text, so any leak of that text is visible in the payload


def run(rec, *, summary, people=(), objects=None, frames=None, labels=None, hint=None, t=0.0, events=(),
        transcript=f"{MARK} said good night", audio=({"label": f"{MARK} snoring", "confidence": 0.7},)):
    frames = frames if frames is not None else [[("laptop-1", BOX_L)]]
    labels = labels if labels is not None else {"laptop-1": "laptop"}
    objects = objects if objects is not None else [obj("laptop", BOX_L, description=f"laptop by the {MARK} bag")]
    w = scene_window(frames, t0=T0 + timedelta(seconds=t))
    omni = OmniResult(status="ok",
                      observation=observation(objects, people=list(people), events=list(events),
                                              scene_summary=summary, location_hint=hint, transcript=transcript,
                                              audio_events=list(audio)),
                      model="fake-omni", raw='{"raw": "reply"}')
    out = rec.process(w, omni, detections_for(frames, labels))
    assert out.status == "posted", out.error
    return out.payload, out.response


def assert_no_text(payload):
    w = payload["window"]
    assert w["private"] is True
    assert w["scene_summary"] is None and w["transcript"] is None and w["audio_events"] == []
    assert "cue" not in w["privacy"]
    for a in payload["assignments"]:
        assert a["sighting"]["description"] is None
        if a.get("new_entity"):
            assert a["new_entity"]["description"] is None
    for ev in payload["events"]:
        assert ev["actor_desc"] is None
    wire = json.dumps(payload).lower()
    assert MARK not in wire, wire
    for word in ("sleeping", "asleep", "bunk", "quarters"):  # neither the summary, the appearance, nor the cue word
        assert word not in wire, word


SLEEP_PERSON = {"ref": "P1", "appearance": f"dark hair, sleeping in a blue sleeping bag, {MARK}"}


def test_ac_per_43_sleep_cue_window_is_private_and_carries_no_text(make_reconciler, inv, storage):
    payload, _ = run(make_reconciler(HARMONY_CAM), summary=f"A person is sleeping in the Harmony Module {MARK}",
                     people=[SLEEP_PERSON], hint="Harmony")
    assert_no_text(payload)
    w = payload["window"]
    assert w["privacy_reason"] == "sleep_cue"
    assert w["privacy"] == {"no_capture": True, "reason": "sleep_cue", "field": "people[0].appearance"}
    assert storage.objects == {}
    # the known object is still a location-only fact
    [a] = payload["assignments"]
    assert a["new_entity"]["catalog_id"] == "laptop" and a["new_entity"]["label"] == "laptop"
    assert a["sighting"]["location_id"] and a["sighting"].get("frame_uri") is None


@pytest.mark.parametrize("kwargs", [
    {"hint": "NOD2S5", "summary": f"crew area {MARK}"},  # a no_capture hinted location
    {"summary": f"view into the crew quarters {MARK}"},  # the summary names a private place
])
def test_ac_per_43_private_zone_window_carries_no_text(make_reconciler, inv, storage, kwargs):
    payload, _ = run(make_reconciler("cam-handheld"), **kwargs)
    assert_no_text(payload)
    assert payload["window"]["privacy_reason"] == "private_zone"
    assert payload["window"]["privacy"]["reason"] == "private_zone"


def test_ac_per_43_privacy_hold_window_carries_no_text(make_reconciler, inv, storage):
    rec = make_reconciler(HARMONY_CAM)
    run(rec, summary="crew in the module", people=[SLEEP_PERSON])
    p2, _ = run(rec, summary=f"a crew member resting in a bag {MARK}",
                people=[{"ref": "P1", "appearance": f"resting in a blue bag, {MARK}"}], t=4)
    assert_no_text(p2)
    assert p2["window"]["privacy_reason"] == "privacy_hold"


def test_ac_per_43_existing_entity_sighted_privately_gets_no_description(make_reconciler, inv, storage):
    # two stale laptops in the module and no detector box: the decision model is asked (AC-PER-29)
    for ago in (1000, 900):
        inv.add_entity(catalog_id="laptop", last_location="NOD2", last_seen_at=T0 - timedelta(seconds=ago),
                       description="black laptop")
    payload, _ = run(make_reconciler(HARMONY_CAM), summary="crew member asleep", people=[SLEEP_PERSON],
                     objects=[obj("ThinkPad", BOX_L, description=f"ThinkPad near the {MARK} bunk")],
                     frames=[[]], labels={})
    assert_no_text(payload)
    assert payload["assignments"], "the known object must still be sighted"
    labelled = [d for d in payload["decisions"] if "label" in d["subject"]]
    assert labelled, payload["decisions"]
    assert all(d["subject"]["label"] == "laptop" for d in labelled)  # the catalog class, never the omni label


def test_ac_per_43_no_person_and_no_actor_in_a_private_window(make_reconciler, inv, storage):
    """AC-PER-36 re-verified on the private path: nobody is tracked and events carry no actor."""
    ev = {"type": "used", "object_label": "laptop", "actor_ref": "P2", "frame_index": 0, "confidence": 0.8,
          "evidence": f"{MARK} typed"}
    payload, _ = run(make_reconciler(HARMONY_CAM), summary="two crew members", hint="Harmony",
                     frames=[[("laptop-1", BOX_L), ("astro-1", BOX_A)]],
                     labels={"laptop-1": "laptop", "astro-1": "astronaut"},
                     objects=[obj("laptop", BOX_L, description=f"laptop {MARK}"),
                              obj("astronaut", BOX_A, description=f"astronaut {MARK}")],
                     people=[SLEEP_PERSON, {"ref": "P2", "appearance": f"blue shirt {MARK}"}], events=[ev])
    assert_no_text(payload)
    assert [a["new_entity"]["kind"] for a in payload["assignments"]] == ["object"]
    assert all(ev["actor_desc"] is None for ev in payload["events"])


def test_ac_per_43_capturable_window_keeps_its_text_and_is_marked_not_private(make_reconciler, inv, storage):
    payload, _ = run(make_reconciler(LAB_CAM), summary="crew working at the rack",
                     transcript="moving the drill", audio=({"label": "speech", "confidence": 0.9},))
    w = payload["window"]
    assert w["private"] is False and w["privacy_reason"] is None
    assert w["scene_summary"] == "crew working at the rack" and w["transcript"] == "moving the drill"
    assert w["audio_events"] == [{"label": "speech", "confidence": 0.9}]
    assert payload["assignments"][0]["sighting"]["description"] == f"laptop by the {MARK} bag"


# ------------------------------------------------------------------ AC-PER-44 a person's own movement has no actor
def test_ac_per_44_person_moving_itself_gets_no_actor(make_reconciler, inv, storage):
    """Live: "person 87dbd193 moved by person (spacesuit, dark visor)". A person track's own movement names no
    other person as the actor; only a person handling an object is an actor."""
    frames = [[("laptop-1", BOX_L), ("astro-1", BOX_A)]]
    events = [
        {"type": "moved", "object_label": "astronaut", "actor_ref": "P1", "frame_index": 0, "confidence": 0.8},
        {"type": "used", "object_label": "laptop", "actor_ref": "P1", "frame_index": 0, "confidence": 0.8},
    ]
    payload, _ = run(make_reconciler(LAB_CAM), summary="crew working", frames=frames,
                            labels={"laptop-1": "laptop", "astro-1": "astronaut"},
                            objects=[obj("laptop", BOX_L), obj("astronaut", BOX_A, description="astronaut")],
                            people=[{"ref": "P1", "appearance": "spacesuit, dark visor"}], events=events,
                            transcript="", audio=())
    kinds = {a["ref"]: (a.get("new_entity") or {}).get("kind") for a in payload["assignments"]}
    by_type = {e["event_type"]: e for e in payload["events"]}
    assert kinds[by_type["moved"]["entity_ref"]] == "person"
    assert by_type["moved"]["actor_desc"] is None
    assert kinds[by_type["used"]["entity_ref"]] == "object"
    assert by_type["used"]["actor_desc"] == "spacesuit, dark visor"
