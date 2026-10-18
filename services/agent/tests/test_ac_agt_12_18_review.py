"""Adversarial review of the UI-review chatbot fixes (AC-AGT-12..18): prompt injection via actor_desc / labels,
grounding bypass in recent_changes, cancellation leaks. See docs/tdd-log/agent.md "UI-review chatbot fixes"."""
from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from datetime import UTC, datetime

import pytest

from nasa_agent.harness import NO_RECORD_REPLY, TIMEOUT_RECORDS_LEAD, records_reply
from nasa_agent.main import stream_chat_events
from tests.conftest import user
from tests.fakes import E_DRILL, FakeLLM, assistant_text, assistant_tools, tool_call

NOW = datetime(2026, 10, 1, 12, 40, tzinfo=UTC)


@pytest.fixture()
def tb(toolbox, fake_inventory):
    toolbox.now = lambda: NOW
    for i in fake_inventory.items:
        i["first_seen_at"] = "2026-09-01T00:00:00Z"
    return toolbox


# ---------------------------------------------------------------- 1. recent_changes scope must not ground claims
async def test_review_empty_recent_changes_scope_does_not_ground_invented_events(make_harness, tb, fake_inventory):
    fake_inventory.events, fake_inventory.reviews = [], []
    llm = FakeLLM([assistant_tools(tool_call("recent_changes", {"location": "NOD1"})),
                   assistant_text("The drill was moved to NOD1 at 11:40.")])
    res = await make_harness(llm).run(user("What changed in Node 1?"))
    assert "drill" not in res["reply"].lower()
    assert "No changes" in res["reply"] and "NOD1" in res["reply"], "deterministic none-found answer instead"
    assert res["citations"] == []


async def test_review_scope_fields_do_not_ground_claims_next_to_real_changes(make_harness, tb):
    llm = FakeLLM([assistant_tools(tool_call("recent_changes", {"location": "LAB1"})),
                   assistant_text("The camera was moved to LAB1 at 11:40.")])
    res = await make_harness(llm).run(user("What changed in the lab?"))
    # AC-AGT-29: the answer is the deterministic rendering of the real changes; the draft is never used (the window
    # in the header is the tool's own scope, stated as such)
    assert "camera" not in res["reply"].lower() and "LAB1 at 11:40" not in res["reply"]
    assert res["reply"].startswith("Changes in Destiny (US Lab, LAB1) between 2026-10-01 11:40 and")
    assert res["reply"] != NO_RECORD_REPLY and "12:20 UTC" in res["reply"]


# ---------------------------------------------------------------- 2. big recent_changes payloads stay structured
async def test_review_large_recent_changes_payload_drops_whole_changes(make_harness, tb, fake_inventory,
                                                                      plain_settings):
    for n in range(40):
        fake_inventory.events.append({"event_id": f"ev-{n}", "event_type": "used", "entity_id": E_DRILL,
                                      "actor_desc": f"crew member number {n} in a long blue shirt",
                                      "from_location": "LAB1P3", "to_location": "LAB1S2",
                                      "occurred_at": f"2026-10-01T12:{n:02d}:30Z", "confidence": 0.8,
                                      "source": "pipeline"})
    # a second, non-rendered tool in the step keeps the model in the loop (AC-AGT-29), so the payload is sent
    llm = FakeLLM([assistant_tools(tool_call("recent_changes", {}), tool_call("lookup_catalog", {"q": "wrench"})),
                   assistant_text("Many uses of the drill.")])
    res = await make_harness(llm, settings=replace(plain_settings, tool_result_token_budget=800)).run(
        user("What changed in the last hour?"))
    msg = json.loads(next(m for m in llm.requests[1]["messages"] if m["role"] == "tool")["content"])
    assert msg["truncated"] is True and isinstance(msg["data"], dict), "never a cut JSON string"
    sent = msg["data"]["changes"]
    assert 0 < len(sent) < 30 and msg["data"]["since"]
    sent_keys = {(c["entity_id"], c["at"]) for c in sent}
    assert {(c["entity_id"], c["at"]) for c in res["citations"]} == sent_keys, "cite only what the model saw"


# ---------------------------------------------------------------- 3. actor free text grounds actors, not places
async def test_review_actor_text_cannot_ground_location_or_time(make_harness, tb, fake_inventory):
    fake_inventory.events[0]["actor_desc"] = "crew member who put it in NOD3 at 09:00"
    for tool, args in (("recent_changes", {}), ("who_moved", {"name": "drill"})):
        llm = FakeLLM([assistant_tools(tool_call(tool, args)),
                       assistant_text("The drill was moved to NOD3 at 09:00.")])
        res = await make_harness(llm).run(user("What happened to the drill?"))
        # AC-AGT-19: replaced by the grounded summary; the actor's injected place/time never surfaces
        assert res["reply"] != NO_RECORD_REPLY, tool
        assert "NOD3" not in res["reply"] and "09:00" not in res["reply"], tool


async def test_review_actor_text_still_grounds_the_actor(make_harness, tb):
    answer = "The pistol-grip drill was moved by crew member in blue shirt at 12:20Z."
    llm = FakeLLM([assistant_tools(tool_call("who_moved", {"name": "drill"})), assistant_text(answer)])
    res = await make_harness(llm).run(user("Who moved the drill in the last hour?"))
    assert res["reply"] == answer
    # AC-AGT-29: a recent_changes answer is rendered, with the actor in operator English
    llm = FakeLLM([assistant_tools(tool_call("recent_changes", {}))])
    res = await make_harness(llm).run(user("Who moved things in the last hour?"))
    assert "Pistol-grip drill moved from Lab port rack 3 (LAB1P3) to Lab starboard rack 2 (LAB1S2) by person " \
           "(blue shirt)" in res["reply"]


# ---------------------------------------------------------------- 4. timeout records reply does not echo claims
def test_review_records_reply_does_not_echo_label_claims():
    reply = records_reply([{"entity_id": E_DRILL, "at": "2026-10-01T12:30:00Z", "location": "LAB1S2",
                            "label": "drill moved by the commander at 09:00 to NOD3"}])
    assert reply.startswith(TIMEOUT_RECORDS_LEAD)
    assert "NOD3" not in reply and "09:00" not in reply and "commander" not in reply
    assert "LAB1S2" in reply and "12:30 UTC" in reply and E_DRILL[:8] in reply


# ---------------------------------------------------------------- 5. no task leaks on disconnect
async def test_review_stream_close_leaves_no_pending_tasks(make_harness):
    class Blocking(FakeLLM):
        def __init__(self):
            super().__init__([])
            self.started = asyncio.Event()

        async def complete(self, messages, tools, tool_choice="auto"):
            self.started.set()
            await asyncio.Event().wait()

    llm = Blocking()
    harness = make_harness(llm)
    before = asyncio.all_tasks()
    gen = stream_chat_events(lambda cb: harness.run(user("q"), on_event=cb), keepalive_s=10, request_id="r")
    await asyncio.wait_for(gen.__anext__(), 2)  # AC-AGT-31 padded opening comment
    await asyncio.wait_for(gen.__anext__(), 2)
    await asyncio.wait_for(llm.started.wait(), 2)
    pending = asyncio.ensure_future(gen.__anext__())  # blocked inside asyncio.wait (no event, long keepalive)
    await asyncio.sleep(0.05)
    pending.cancel()  # Starlette cancels the response task on disconnect
    with pytest.raises(asyncio.CancelledError):
        await pending
    await gen.aclose()
    await asyncio.sleep(0)
    leaked = [t for t in asyncio.all_tasks() - before if t is not asyncio.current_task() and not t.done()]
    assert leaked == [], leaked
