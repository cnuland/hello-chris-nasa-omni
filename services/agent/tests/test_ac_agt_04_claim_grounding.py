"""AC-AGT-4 grounding, per claim (review round 1).

Only tools that return citable inventory facts ground a turn (where_is, get_item, item_history, who_moved,
search_items, list_unknowns, search_observations). ``inventory_stats`` (counts), ``recall_memory`` (stale background)
and ``propose_update`` (a pending suggestion) never do. In a grounded turn every location id / module name, clock time,
date and "moved by <actor>" phrase in the reply must appear in this turn's grounding tool payloads, otherwise the reply
is replaced by the no-record answer and no citations are returned.
"""
from __future__ import annotations

from tests.conftest import user
from tests.fakes import E_CAMERA, E_DRILL, FakeLLM, assistant_text, assistant_tools, tool_call
from tests.test_ac_agt_04_grounding import is_no_record

INVENTED = "The drill is in NOD3, moved by the commander at 09:00."


async def test_ac_agt_04_stats_then_invented_location_is_rejected(make_harness):
    llm = FakeLLM([assistant_tools(tool_call("inventory_stats", {})), assistant_text(INVENTED)])
    res = await make_harness(llm).run(user("where is the drill?"))
    assert is_no_record(res["reply"]), res["reply"]
    assert "NOD3" not in res["reply"] and res["citations"] == []


async def test_ac_agt_04_memory_only_answer_is_rejected(make_harness):
    llm = FakeLLM([
        assistant_tools(tool_call("recall_memory", {"scope": "entity", "key": E_DRILL})),
        assistant_text("The drill is in LAB1S2."),
    ])
    res = await make_harness(llm).run(user("where is the drill?"))
    assert is_no_record(res["reply"]), res["reply"]
    assert res["citations"] == []


async def test_ac_agt_04_memory_only_invented_answer_is_rejected(make_harness):
    llm = FakeLLM([
        assistant_tools(tool_call("recall_memory", {"scope": "global", "key": "global"})),
        assistant_text(INVENTED),
    ])
    res = await make_harness(llm).run(user("where is the drill?"))
    assert is_no_record(res["reply"]) and res["citations"] == []


async def test_ac_agt_04_proposal_alone_does_not_ground_facts(make_harness):
    llm = FakeLLM([
        assistant_tools(tool_call("propose_update", {"entity_id": E_DRILL, "fields": {"label": "cordless drill"}})),
        assistant_text(INVENTED),
    ])
    res = await make_harness(llm).run(user("rename the drill"))
    assert "NOD3" not in res["reply"] and "commander" not in res["reply"]
    assert res["citations"] == []
    assert len(res["proposals"]) == 1, "the proposal itself is still surfaced"


async def test_ac_agt_04_where_is_drill_then_claim_about_another_item_location(make_harness):
    llm = FakeLLM([
        assistant_tools(tool_call("where_is", {"name": "drill"})),
        assistant_text("The Nikon camera is in NOD2D4, last seen at 11:05."),
    ])
    res = await make_harness(llm).run(user("where is the drill and the camera?"))
    assert is_no_record(res["reply"]), res["reply"]
    assert res["citations"] == []


async def test_ac_agt_04_invented_time_in_grounded_turn_is_rejected(make_harness):
    llm = FakeLLM([
        assistant_tools(tool_call("where_is", {"name": "drill"})),
        assistant_text("The pistol-grip drill was last seen in LAB1S2 at 09:45Z."),
    ])
    res = await make_harness(llm).run(user("where is the drill?"))
    assert is_no_record(res["reply"]), res["reply"]


async def test_ac_agt_04_invented_module_name_is_rejected(make_harness):
    llm = FakeLLM([
        assistant_tools(tool_call("where_is", {"name": "drill"})),
        assistant_text("The pistol-grip drill is in Node 3."),
    ])
    res = await make_harness(llm).run(user("where is the drill?"))
    assert is_no_record(res["reply"]), res["reply"]


async def test_ac_agt_04_invented_actor_is_rejected(make_harness):
    llm = FakeLLM([
        assistant_tools(tool_call("who_moved", {"name": "camera"})),
        assistant_text("The camera was moved by the commander at 11:00Z."),
    ])
    res = await make_harness(llm).run(user("who moved the camera?"))
    assert is_no_record(res["reply"]), res["reply"]
    assert "commander" not in res["reply"]


async def test_ac_agt_04_supported_claims_are_kept(make_harness):
    llm = FakeLLM([
        assistant_tools(tool_call("who_moved", {"name": "camera"})),
        assistant_text("The Nikon camera was moved from LAB1P3 to NOD2D4 by a crew member in a red shirt at "
                       "11:00Z on 2026-10-01."),
    ])
    res = await make_harness(llm).run(user("who moved the camera?"))
    assert res["reply"].startswith("The Nikon camera was moved from LAB1P3 to NOD2D4")
    assert any(c.get("entity_id") == E_CAMERA for c in res["citations"])


async def test_ac_agt_04_stats_plus_where_is_keeps_grounded_answer(make_harness):
    llm = FakeLLM([
        assistant_tools(tool_call("inventory_stats", {}, "s"), tool_call("where_is", {"name": "drill"}, "w")),
        assistant_text("There are 2 unknown items; the drill is in LAB1S2."),
    ])
    res = await make_harness(llm).run(user("stats and drill?"))
    assert not is_no_record(res["reply"])
    assert {c.get("entity_id") for c in res["citations"]} == {E_DRILL}
