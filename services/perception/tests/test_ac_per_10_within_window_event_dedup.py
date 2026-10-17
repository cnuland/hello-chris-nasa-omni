"""AC-PER-10 amendment (cluster integration 2026-10-17): one real omni reply listed the same `used` event three times
(same drill, same actor "Astronaut in blue space suit", same frame; confidence 0.7/0.5/0.4), and all three were stored.
The overlap dedup only compared against earlier windows. Repeats inside one reply now collapse to one event (the most
confident); different actors, types or destinations stay separate."""
from __future__ import annotations

from support import obj
from test_ac_per_08_reconciler import BOX_A, process
from test_ac_per_09_10_fungible_events import MOVE_PEOPLE

TWO_PEOPLE = MOVE_PEOPLE + [{"ref": "P2", "appearance": "crew member, red shirt", "activity": "watching"}]


def _used(conf, actor="P1", frame_index=0, etype="used", to=None):
    return {"type": etype, "object_label": "drill", "actor_ref": actor, "to_location_hint": to,
            "frame_index": frame_index, "evidence": "crew using the drill", "confidence": conf}


def _run(make_reconciler, inv, events, people=MOVE_PEOPLE):
    process(make_reconciler(), [[("drill-1", BOX_A)]], [obj("drill", BOX_A)], {"drill-1": "drill"},
            people=people, events=events)
    return inv.last["events"]


def test_repeated_identical_events_in_one_reply_collapse_to_the_most_confident(make_reconciler, inv):
    evs = _run(make_reconciler, inv, [_used(0.7), _used(0.5), _used(0.4)])
    used = [e for e in evs if e["event_type"] == "used"]
    assert len(used) == 1
    assert used[0]["confidence"] == 0.7


def test_repeats_at_different_frames_of_one_window_also_collapse(make_reconciler, inv):
    # the window spans a few seconds; the same action reported at frames 0 and 0 (single-frame window) or later frames
    evs = _run(make_reconciler, inv, [_used(0.4), _used(0.9)])
    assert [e["confidence"] for e in evs if e["event_type"] == "used"] == [0.9]


def test_different_actors_stay_separate(make_reconciler, inv):
    evs = _run(make_reconciler, inv, [_used(0.7, actor="P1"), _used(0.6, actor="P2")], people=TWO_PEOPLE)
    assert len([e for e in evs if e["event_type"] == "used"]) == 2


def test_different_types_stay_separate(make_reconciler, inv):
    evs = _run(make_reconciler, inv, [_used(0.7), _used(0.6, etype="handed_over")])
    assert sorted(e["event_type"] for e in evs) == ["handed_over", "used"]
