"""AC-AGT-18 (docs/QUESTIONS.md #21): the "usual stowage" safety rewrite replaced the whole reply and dropped the rest
of the answer ("how many should we have": expected_count). It now replaces only the offending sentences and appends
the provable catalog fields."""
from __future__ import annotations

from nasa_agent.grounding import USUAL_LOCATION_LEAD
from tests.conftest import user
from tests.fakes import E_CAMERA, FakeLLM, assistant_text, assistant_tools, tool_call


async def test_ac_agt_18_rewrite_keeps_other_sentences_and_adds_expected_count(make_harness):
    llm = FakeLLM([
        assistant_tools(tool_call("lookup_catalog", {"q": "multimeter"})),
        assistant_text("The multimeter is in LAB1 right now. We should have 1 on board."),
    ])
    res = await make_harness(llm).run(user("Where is the multimeter and how many should we have?"))
    reply = res["reply"]
    assert reply.startswith(USUAL_LOCATION_LEAD)
    assert "usually kept in LAB1" in reply
    assert "catalog expected count: 1" in reply
    assert "We should have 1 on board." in reply
    assert "right now" not in reply


async def test_ac_agt_18_fungible_entries_say_stock_counts(make_harness):
    llm = FakeLLM([
        assistant_tools(tool_call("lookup_catalog", {"q": "food pack"})),
        assistant_text("The food packs are in NOD1 now."),
    ])
    res = await make_harness(llm).run(user("where are the food packs?"))
    assert "usually kept in NOD1" in res["reply"] and "tracked as stock counts" in res["reply"]
    assert " now." not in res["reply"]


async def test_ac_agt_18_dangling_current_sentence_is_dropped(make_harness):
    llm = FakeLLM([
        assistant_tools(tool_call("lookup_catalog", {"q": "drill"})),
        assistant_text("The drill is in LAB1. It is there right now. Two are expected on board."),
    ])
    res = await make_harness(llm).run(user("where is the drill?"))
    reply = res["reply"]
    assert "It is there right now" not in reply
    assert "Two are expected on board." in reply and "catalog expected count: 2" in reply


async def test_ac_agt_18_kept_sentences_keep_their_entity_citations(make_harness):
    llm = FakeLLM([
        assistant_tools(tool_call("who_moved", {"name": "camera"}, "w"), tool_call("lookup_catalog", {"q": "drill"}, "c")),
        assistant_text("The drill is in LAB1 now. The Nikon camera was moved by crew member, red shirt at 11:00Z."),
    ])
    res = await make_harness(llm).run(user("where is the drill and who moved the camera?"))
    assert "The Nikon camera was moved by crew member, red shirt at 11:00Z." in res["reply"]
    assert any(c.get("entity_id") == E_CAMERA for c in res["citations"])
    assert any(c.get("catalog_id") == "drill-pistol" and not c.get("entity_id") for c in res["citations"])


async def test_ac_agt_18_only_home_catalog_citations_when_nothing_else_is_kept(make_harness):
    llm = FakeLLM([
        assistant_tools(tool_call("where_is", {"name": "drill"}, "w"), tool_call("lookup_catalog", {"q": "drill"}, "c")),
        assistant_text("The drill is in LAB1 now."),
    ])
    res = await make_harness(llm).run(user("where is the drill now?"))
    assert all(c.get("catalog_id") == "drill-pistol" and not c.get("entity_id") for c in res["citations"])
