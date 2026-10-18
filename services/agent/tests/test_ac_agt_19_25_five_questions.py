"""UI review round 2: the standard five questions in ONE conversation (the dashboard re-sends the growing transcript),
scripted with the deployed model's real behaviour and the live data shapes (tests/live_round2.py).

Live before the fix (2026-10-18 01:3x UTC): Q1 cut off mid-word, Q3 "no record", Q4 scoped to USOS (reviewer run),
Q5 "no record"."""
from __future__ import annotations

from nasa_agent.grounding import USUAL_LOCATION_LEAD
from nasa_agent.harness import NO_RECORD_REPLY
from tests.fakes import FakeLLM, assistant_text, assistant_tools, tool_call
from tests.live_round2 import DRILL_USED, LIVE_DRILL_DRAFT_1, LIVE_LAPTOP_CUT, load_live

QUESTIONS = [
    "Where is the laptop right now?",
    "And where is the multimeter usually kept?",
    "Who used the drill?",
    "What changed in the last hour?",
    "What unknown objects are in Node 1?",
]
SCRIPTS = [
    [assistant_tools(tool_call("where_is", {"name": "laptop"})),
     {"role": "assistant", "content": LIVE_LAPTOP_CUT, "finish_reason": "length"}],
    [assistant_tools(tool_call("lookup_catalog", {"q": "multimeter"})),
     assistant_text("The Fluke multimeter is usually kept in LAB1 (Destiny (US Lab)). Expected count: 1.")],
    [assistant_tools(tool_call("who_moved", {"name": "drill", "event_types": ["used"]})),
     assistant_text(LIVE_DRILL_DRAFT_1)],
    # the reviewer's run: a 2 h window and the laptop answer's location carried over
    [assistant_tools(tool_call("recent_changes", {"since": "2026-10-17T23:20:16Z", "location": "USOS"})),
     assistant_text("No changes were recorded in the last hour in the USOS location.")],
    [assistant_tools(tool_call("list_unknowns", {"location": "Node 1"})),
     assistant_text("I don't have a record of that.")],
]


async def test_ui2_five_questions_one_conversation(make_harness, fake_inventory, toolbox):
    load_live(fake_inventory, toolbox)
    messages: list[dict] = []
    results = []
    for question, script in zip(QUESTIONS, SCRIPTS, strict=True):
        messages.append({"role": "user", "content": question})
        r = await make_harness(FakeLLM(script)).run(list(messages))
        results.append(r)
        messages.append({"role": "assistant", "content": r["reply"]})

    q1, q2, q3, q4, q5 = results
    # Q1 (UI2-03, UI2-12): trimmed at a line, numbered 1..3, announced
    assert q1["reply"].endswith("(showing the first 3 of 5; ask for more)")
    assert "**Conf" not in q1["reply"] and "\n2. " in q1["reply"] and "\n3. " in q1["reply"]
    assert "T22:51" not in q1["reply"] and "2026-10-17 22:51 UTC" in q1["reply"]
    # Q2: usual stowage from the catalog
    assert "LAB1" in q2["reply"] and NO_RECORD_REPLY not in q2["reply"]
    assert q2["reply"].startswith(USUAL_LOCATION_LEAD) or "usually kept" in q2["reply"]
    # Q3 (UI2-01): the actor is named and cited
    assert q3["reply"] != NO_RECORD_REPLY and "blue shirt" in q3["reply"]
    assert any(c["entity_id"] == DRILL_USED for c in q3["citations"])
    # Q4 (UI2-02, UI2-04): station-wide, exact one-hour window from the injected clock
    assert q4["tool_calls"][0]["arguments"] == {"since": "2026-10-18T00:20:00Z"}
    assert "USOS" not in q4["reply"]
    # Q5 (UI2-08): "Node 1" resolved; an empty result answered honestly
    assert q5["reply"] == "There are no unknown objects in Node 1 Unity (NOD1)."
