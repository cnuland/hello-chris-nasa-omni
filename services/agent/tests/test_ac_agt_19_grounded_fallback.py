"""AC-AGT-19 (UI review round 2, UI2-01 P1): "Who used the drill?" answered "I don't have a record of that." although
who_moved returned the live `used` event of drill a454e74e by "in blue shirt, headset".

Root cause (reproduced live in the agent pod): the deployed model wrote "used by a crew member in a blue shirt with a
headset" / "used by a crew member described as \"in blue shirt, headset\"". The actor claim check requires every
content word of the actor phrase to occur in the evidence; "crew", "member" (the wording the system prompt itself
asks for) and "described" do not, so the claim was rejected and the reply silently replaced by NO_RECORD_REPLY."""
from __future__ import annotations

import json
import logging

from nasa_agent.grounding import USUAL_LOCATION_LEAD, evidence_text, unsupported_claims
from nasa_agent.harness import NO_RECORD_REPLY
from nasa_agent.tools import evidence_view
from tests.conftest import user
from tests.fakes import FakeLLM, assistant_text, assistant_tools, tool_call
from tests.live_round2 import DRILL_USED, LIVE_DRILL_DRAFT_1, LIVE_DRILL_DRAFT_2, LIVE_DRILL_EVENT, load_live

WHO = tool_call("who_moved", {"name": "drill", "event_types": ["used"]})


def _drill_evidence():
    data = [{"entity_id": DRILL_USED, "label": "drill", "last_location": "USOS",
             "moves": [{k: LIVE_DRILL_EVENT[k] for k in ("event_id", "event_type", "actor_desc", "from_location",
                                                         "to_location", "occurred_at", "confidence", "source")}]}]
    return evidence_text([evidence_view("who_moved", data)])


# ---------------------------------------------------------------- the claim check itself
def test_ac_agt_19_live_drafts_pass_the_claim_check():
    ev = _drill_evidence()
    assert unsupported_claims(LIVE_DRILL_DRAFT_1, ev) == []
    assert unsupported_claims(LIVE_DRILL_DRAFT_2, ev) == []


def test_ac_agt_19_role_words_do_not_hide_wrong_appearance():
    ev = _drill_evidence()
    assert unsupported_claims("The drill was used by a crew member in a red shirt at 22:53.", ev)
    assert unsupported_claims("The drill was used by the commander at 22:53.", ev)


# ---------------------------------------------------------------- end to end with the scripted LLM
async def test_ac_agt_19_who_used_the_drill_live_draft_is_answered(make_harness, fake_inventory, toolbox):
    load_live(fake_inventory, toolbox)
    for draft in (LIVE_DRILL_DRAFT_1, LIVE_DRILL_DRAFT_2):
        r = await make_harness(FakeLLM([assistant_tools(WHO), assistant_text(draft)])).run(user("Who used the drill?"))
        assert r["reply"] != NO_RECORD_REPLY
        assert "blue shirt" in r["reply"]
        assert any(c["entity_id"] == DRILL_USED for c in r["citations"])


async def test_ac_agt_19_rejected_wording_falls_back_to_grounded_summary(make_harness, fake_inventory, toolbox):
    load_live(fake_inventory, toolbox)
    bad = "The drill was used by the commander in a green suit at 21:10 UTC in NOD3."
    r = await make_harness(FakeLLM([assistant_tools(WHO), assistant_text(bad)])).run(user("Who used the drill?"))
    reply = r["reply"]
    assert reply != NO_RECORD_REPLY
    assert "commander" not in reply and "NOD3" not in reply and "21:10" not in reply
    # AC-AGT-29 operator English (no pasted ids; the citation carries the entity)
    assert "Drill used in US Orbital Segment (USOS) by person (blue shirt, headset)" in reply
    assert "22:53 UTC" in reply and "USOS" in reply
    assert unsupported_claims(reply, _drill_evidence()) == [], "the fallback itself is grounded"
    assert any(c["entity_id"] == DRILL_USED for c in r["citations"]), "the fallback keeps the citations"


async def test_ac_agt_19_fallback_lists_at_most_five_and_says_so(make_harness, fake_inventory, toolbox):
    load_live(fake_inventory, toolbox)
    for k in range(7):
        fake_inventory.events.append({**LIVE_DRILL_EVENT, "event_id": f"ev-extra-{k}",
                                      "occurred_at": f"2026-10-17T22:0{k}:00Z"})
    bad = "The drill was used by the commander at 21:10."
    r = await make_harness(FakeLLM([assistant_tools(WHO), assistant_text(bad)])).run(user("Who used the drill?"))
    assert "(showing the first 5 of 8; ask for more)" in r["reply"]


async def test_ac_agt_19_fallback_scrubs_claims_out_of_actor_text(make_harness, fake_inventory, toolbox):
    load_live(fake_inventory, toolbox)
    fake_inventory.events[-2]["actor_desc"] = "crew member who put it in NOD3 at 09:00"
    bad = "The drill was used by the commander."
    r = await make_harness(FakeLLM([assistant_tools(WHO), assistant_text(bad)])).run(user("Who used the drill?"))
    assert r["reply"] != NO_RECORD_REPLY
    assert "NOD3" not in r["reply"] and "09:00" not in r["reply"]


async def test_ac_agt_19_fallback_label_with_claims_uses_short_id(make_harness, fake_inventory, toolbox):
    load_live(fake_inventory, toolbox)
    for i in fake_inventory.items:
        if i["entity_id"] == DRILL_USED:
            i["label"] = "drill moved to NOD3 at 09:00"
    bad = "The drill was used by the commander."
    r = await make_harness(FakeLLM([assistant_tools(WHO), assistant_text(bad)])).run(user("Who used the drill?"))
    assert "NOD3" not in r["reply"] and "09:00" not in r["reply"] and "a454e74e" in r["reply"]


async def test_ac_agt_19_catalog_only_rejection_gives_usual_location_reply(make_harness):
    llm = FakeLLM([assistant_tools(tool_call("lookup_catalog", {"q": "multimeter"})),
                   assistant_text("The multimeter was moved by the commander at 11:11.")])
    r = await make_harness(llm).run(user("Where is the multimeter usually kept?"))
    assert r["reply"].startswith(USUAL_LOCATION_LEAD)
    assert "LAB1" in r["reply"] and "11:11" not in r["reply"]
    assert any(c.get("catalog_id") == "multimeter" for c in r["citations"])


async def test_ac_agt_19_turn_log_marks_the_fallback(make_harness, fake_inventory, toolbox, caplog):
    caplog.set_level(logging.INFO, logger="nasa_agent.harness")
    load_live(fake_inventory, toolbox)
    bad = "The drill was used by the commander in a green suit at 21:10 UTC."
    await make_harness(FakeLLM([assistant_tools(WHO), assistant_text(bad)])).run(user("Who used the drill?"))
    rec = next(r for r in caplog.records if r.getMessage() == "chat turn")
    assert rec.fallback == "grounded_summary"
    assert rec.unsupported_claims >= 1
    assert "commander" not in json.dumps(rec.__dict__, default=str), "never the claim text"


async def test_ac_agt_19_no_data_is_still_no_record(make_harness):
    llm = FakeLLM([assistant_tools(tool_call("who_moved", {"name": "flux capacitor"})),
                   assistant_text("It was used by the commander at 10:00.")])
    r = await make_harness(llm).run(user("Who used the flux capacitor?"))
    # AC-AGT-39 (round 5): never seen and not in the catalog, said instead of a bare "no record"
    assert r["reply"] == ("No flux capacitor has been seen yet in this run, and the item catalog has no entry for "
                          "it.") and r["citations"] == []
    assert "commander" not in r["reply"]
