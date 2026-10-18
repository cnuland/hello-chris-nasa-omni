"""AC-AGT-20 (UI review round 2, UI2-02 P1): follow-ups silently reused the previous answer's location. Q4 "What
changed in the last hour?" called recent_changes(location="USOS") after the laptop answer mentioned USOS. The harness
now strips a `location` argument of recent_changes / list_unknowns unless the current user message names a place,
and a reply under a location filter states it."""
from __future__ import annotations

from tests.fakes import FakeLLM, assistant_text, assistant_tools, tool_call
from tests.live_round2 import load_live

LAPTOP_ANSWER = "The laptop is currently at USOS (US Orbital Segment), last seen 22:51 UTC."


def _conversation(*turns: str) -> list[dict]:
    """user, assistant, user, ... ending with a user message."""
    return [{"role": "user" if i % 2 == 0 else "assistant", "content": t} for i, t in enumerate(turns)]


def _item_calls_with_location(fake_inventory):
    return [r for r in fake_inventory.calls("/items") if "location" in fake_inventory.params(r)]


async def test_ac_agt_20_follow_up_location_is_stripped(make_harness, fake_inventory, toolbox):
    load_live(fake_inventory, toolbox)
    llm = FakeLLM([assistant_tools(tool_call("recent_changes", {"since": "2026-10-18T00:20:00Z", "location": "USOS"})),
                   assistant_text("No changes were recorded in the last hour.")])
    msgs = _conversation("Where is the laptop right now?", LAPTOP_ANSWER, "What changed in the last hour?")
    r = await make_harness(llm).run(msgs)
    call = r["tool_calls"][0]
    assert call["name"] == "recent_changes" and "location" not in call["arguments"]
    assert _item_calls_with_location(fake_inventory) == []
    assert "USOS" not in r["reply"]


async def test_ac_agt_20_location_named_in_current_message_is_kept(make_harness, fake_inventory, toolbox):
    load_live(fake_inventory, toolbox)
    llm = FakeLLM([assistant_tools(tool_call("recent_changes", {"location": "NOD1"})),
                   assistant_text("Nothing changed.")])
    r = await make_harness(llm).run(_conversation("What changed in Node 1 in the last hour?"))
    assert r["tool_calls"][0]["arguments"].get("location") == "NOD1"


async def test_ac_agt_20_module_alias_and_id_count_as_naming_a_place(make_harness, fake_inventory, toolbox):
    load_live(fake_inventory, toolbox)
    for text in ("What changed in Unity today?", "Any changes in nod1 since 00:30?"):
        llm = FakeLLM([assistant_tools(tool_call("recent_changes", {"location": "NOD1"})), assistant_text("ok")])
        r = await make_harness(llm).run(_conversation(text))
        assert r["tool_calls"][0]["arguments"].get("location") == "NOD1", text
    # review 5: naming a different place does not keep the model's location
    llm = FakeLLM([assistant_tools(tool_call("recent_changes", {"location": "NOD1"})), assistant_text("ok")])
    r = await make_harness(llm).run(_conversation("What changed in Zarya today?"))
    assert "location" not in r["tool_calls"][0]["arguments"]


async def test_ac_agt_20_reference_to_earlier_place_keeps_location(make_harness, fake_inventory, toolbox):
    load_live(fake_inventory, toolbox)
    llm = FakeLLM([assistant_tools(tool_call("list_unknowns", {"location": "USOS"})), assistant_text("ok")])
    msgs = _conversation("Where is the laptop right now?", LAPTOP_ANSWER, "Any unknown objects in that module?")
    r = await make_harness(llm).run(msgs)
    assert r["tool_calls"][0]["arguments"].get("location") == "USOS"


async def test_ac_agt_20_existential_there_is_not_a_place(make_harness, fake_inventory, toolbox):
    load_live(fake_inventory, toolbox)
    llm = FakeLLM([assistant_tools(tool_call("list_unknowns", {"location": "USOS"})), assistant_text("ok")])
    msgs = _conversation("Where is the laptop right now?", LAPTOP_ANSWER, "Are there any unknown objects?")
    r = await make_harness(llm).run(msgs)
    assert "location" not in r["tool_calls"][0]["arguments"]
    assert _item_calls_with_location(fake_inventory) == []


async def test_ac_agt_20_list_unknowns_location_stripped_on_follow_up(make_harness, fake_inventory, toolbox):
    load_live(fake_inventory, toolbox)
    llm = FakeLLM([assistant_tools(tool_call("list_unknowns", {"location": "NOD2"})),
                   assistant_text("There are two unknown objects: unknown white pouch and unknown grey box.")])
    msgs = _conversation("What changed in Node 2?", "Nothing changed in Node 2.", "List the unknown objects.")
    r = await make_harness(llm).run(msgs)
    assert "location" not in r["tool_calls"][0]["arguments"]
    assert _item_calls_with_location(fake_inventory) == []


async def test_ac_agt_20_applied_filter_is_stated_in_reply(make_harness, fake_inventory, toolbox):
    load_live(fake_inventory, toolbox)
    fake_inventory.events.append({"event_id": "ev-n1", "event_type": "moved", "entity_id": "f4bb626b-ca22-4a8d-abaf-"
                                  "44da2c99eb97", "actor_desc": "operator", "from_location": "NOD2",
                                  "to_location": "NOD1", "occurred_at": "2026-10-18T01:00:00Z", "confidence": 1.0,
                                  "evidence_url": None, "source": "operator"})
    llm = FakeLLM([assistant_tools(tool_call("recent_changes", {"location": "Node 1"})),
                   assistant_text("One drill was moved at 01:00 UTC.")])
    r = await make_harness(llm).run(_conversation("What changed in Node 1 in the last hour?"))
    assert r["reply"].startswith("In Node 1 Unity (NOD1): ")
    assert r["tool_calls"][0]["arguments"] == {"location": "Node 1", "since": "2026-10-18T00:20:00Z"}


async def test_ac_agt_20_reply_already_naming_scope_is_unchanged(make_harness, fake_inventory, toolbox):
    load_live(fake_inventory, toolbox)
    llm = FakeLLM([assistant_tools(tool_call("list_unknowns", {"location": "NOD2"})),
                   assistant_text("In Node 2 there is one unknown white pouch (NOD2O4), seen at 10:00 UTC.")])
    r = await make_harness(llm).run(_conversation("Unknown objects in Node 2?"))
    assert r["reply"].startswith("In Node 2 there is")


async def test_ac_agt_20_empty_window_reply_never_claims_a_stripped_scope(make_harness, fake_inventory, toolbox):
    """The model may still word the answer with the stripped scope ("in the USOS location"; USOS has no digit, so the
    lexical claim check cannot see it). An empty window is always answered from its real scope."""
    load_live(fake_inventory, toolbox)
    llm = FakeLLM([assistant_tools(tool_call("recent_changes", {"location": "USOS"})),
                   assistant_text("No changes were recorded in the last hour in the USOS location.")])
    msgs = _conversation("Where is the laptop right now?", LAPTOP_ANSWER, "What changed in the last hour?")
    r = await make_harness(llm).run(msgs)
    assert r["reply"] == "No changes were recorded between 2026-10-18 00:20 and 01:20 UTC."
