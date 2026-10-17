"""AC-AGT-4 grounding: facts must come from tool results in this turn; the harness appends citations from the tools
used; if no tool returned data it answers "I don't have a record of that" instead of guessing. Scripted fake LLM.
"""
from __future__ import annotations

from nasa_agent.harness import NO_RECORD_REPLY
from tests.conftest import user
from tests.fakes import (
    E_CAMERA,
    E_DRILL,
    E_UNK_LAB,
    E_UNK_NOD2,
    W_DRILL,
    FakeLLM,
    assistant_text,
    assistant_tools,
    tool_call,
)

NO_RECORD_PREFIX = "I don't have a record of that"


def is_no_record(reply: str) -> bool:
    return reply.strip().startswith(NO_RECORD_PREFIX)


def test_ac_agt_04_no_record_reply_text():
    assert NO_RECORD_REPLY.startswith(NO_RECORD_PREFIX)


async def test_ac_agt_04_answer_without_any_tool_is_replaced(make_harness):
    llm = FakeLLM([assistant_text("The drill is in Node 3, moved by the commander at 09:00.")])
    res = await make_harness(llm).run(user("where is the drill?"))
    assert is_no_record(res["reply"])
    assert "Node 3" not in res["reply"] and "09:00" not in res["reply"]
    assert res["citations"] == []


async def test_ac_agt_04_tool_returned_nothing_so_guess_is_replaced(make_harness):
    llm = FakeLLM([
        assistant_tools(tool_call("where_is", {"name": "flux capacitor"})),
        assistant_text("The flux capacitor is in LAB1P3."),
    ])
    res = await make_harness(llm).run(user("where is the flux capacitor?"))
    assert is_no_record(res["reply"])
    assert "LAB1P3" not in res["reply"]
    assert res["citations"] == []


async def test_ac_agt_04_upstream_failure_means_no_record(make_harness, fake_inventory):
    fake_inventory.fail_with = 500
    llm = FakeLLM([
        assistant_tools(tool_call("where_is", {"name": "drill"})),
        assistant_text("The drill is probably in LAB1S2."),
    ])
    res = await make_harness(llm).run(user("where is the drill?"))
    assert is_no_record(res["reply"])
    assert res["citations"] == []


async def test_ac_agt_04_only_validation_errors_means_no_record(make_harness):
    llm = FakeLLM([
        assistant_tools(tool_call("get_item", {"entity_id": "not-a-uuid"})),
        assistant_text("It is in LAB1S2."),
    ])
    res = await make_harness(llm).run(user("where is item not-a-uuid?"))
    assert is_no_record(res["reply"])


async def test_ac_agt_04_grounded_answer_kept_and_cited(make_harness):
    llm = FakeLLM([
        assistant_tools(tool_call("where_is", {"name": "drill"})),
        assistant_text("The pistol-grip drill was last seen in LAB1S2 at 12:30Z."),
    ])
    res = await make_harness(llm).run(user("where is the drill?"))
    assert res["reply"].startswith("The pistol-grip drill was last seen in LAB1S2")
    assert any(c.get("entity_id") == E_DRILL and str(c.get("at", "")).startswith("2026-10-01T12:30")
               for c in res["citations"])


async def test_ac_agt_04_citations_come_only_from_tools_used_this_turn(make_harness):
    llm = FakeLLM([
        assistant_tools(tool_call("who_moved", {"name": "camera"})),
        assistant_text("The camera was moved by a crew member in a red shirt at 11:00Z."),
    ])
    res = await make_harness(llm).run(user("who moved the camera?"))
    ids = {c.get("entity_id") for c in res["citations"]}
    assert E_CAMERA in ids
    assert E_DRILL not in ids


async def test_ac_agt_04_previous_turn_facts_do_not_ground_this_turn(make_harness):
    history = [
        {"role": "user", "content": "where is the drill?"},
        {"role": "assistant", "content": f"The drill is in LAB1S2 [{E_DRILL}]."},
        {"role": "user", "content": "and who moved it?"},
    ]
    llm = FakeLLM([assistant_text("A crew member in a blue shirt moved it at 12:20Z.")])
    res = await make_harness(llm).run(history)
    assert is_no_record(res["reply"])
    assert res["citations"] == []


async def test_ac_agt_04_window_citations_from_observation_search(make_harness):
    llm = FakeLLM([
        assistant_tools(tool_call("search_observations", {"q": "drill"})),
        assistant_text("A crew member stowed the drill in the lab rack."),
    ])
    res = await make_harness(llm).run(user("what happened with the drill?"))
    assert any(c.get("window_id") == W_DRILL and c.get("at") for c in res["citations"])


async def test_ac_agt_04_citations_have_exactly_one_id_and_a_time(make_harness):
    llm = FakeLLM([
        assistant_tools(tool_call("list_unknowns", {}), tool_call("search_observations", {"q": "drill"})),
        assistant_text("Two unknowns; the drill was stowed."),
    ])
    res = await make_harness(llm).run(user("summary?"))
    assert res["citations"]
    for c in res["citations"]:
        assert bool(c.get("entity_id")) != bool(c.get("window_id")), c
        assert c.get("at"), c
    ids = {c.get("entity_id") for c in res["citations"]}
    assert {E_UNK_NOD2, E_UNK_LAB} <= ids


async def test_ac_agt_04_citations_are_deduplicated(make_harness):
    llm = FakeLLM([
        assistant_tools(tool_call("where_is", {"name": "drill"}, "a")),
        assistant_tools(tool_call("get_item", {"entity_id": E_DRILL}, "b")),
        assistant_text("The drill is in LAB1S2."),
    ])
    res = await make_harness(llm).run(user("where is the drill?"))
    keys = [(c.get("entity_id"), c.get("window_id"), c.get("at")) for c in res["citations"]]
    assert len(keys) == len(set(keys))
    assert sum(1 for k in keys if k[0] == E_DRILL) >= 1


async def test_ac_agt_04_data_from_any_successful_tool_keeps_answer(make_harness):
    """One failed lookup plus one successful lookup: the answer is grounded by the successful one."""
    llm = FakeLLM([
        assistant_tools(tool_call("where_is", {"name": "flux capacitor"}, "a"), tool_call("where_is", {"name": "drill"},
                                                                                           "b")),
        assistant_text("No flux capacitor on record; the drill is in LAB1S2."),
    ])
    res = await make_harness(llm).run(user("where are the flux capacitor and the drill?"))
    assert not is_no_record(res["reply"])
    assert {c.get("entity_id") for c in res["citations"]} == {E_DRILL}


async def test_ac_agt_04_step_limit_without_final_answer_does_not_guess(make_harness):
    llm = FakeLLM([], default=assistant_tools(tool_call("where_is", {"name": "flux capacitor"})))
    res = await make_harness(llm).run(user("where is the flux capacitor?"))
    assert is_no_record(res["reply"])
