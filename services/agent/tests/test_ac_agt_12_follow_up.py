"""AC-AGT-12: follow-up turns (UI review UI-01, P1).

Live reproduction (2026-10-18, Qwen3-8B-AWQ): history ``[user: where is the laptop?, assistant: "The laptop [...] was
last seen in USOS at 22:51:03Z.", user: where is the multimeter usually kept?]`` -> 0 tool calls, reply "I don't have a
record of that." (3/3, deterministic). The same question asked fresh -> lookup_catalog, grounded answer. Earlier turns'
tool results are never re-sent, so the earlier answer looks like facts stated without tools, and the model copies that
pattern; AC-AGT-4 then (correctly) refuses the ungrounded reply. The scripted LLM below reproduces exactly that
behaviour: in turn 2 its first step answers from the old answer without calling a tool.
"""
from __future__ import annotations

import itertools
from datetime import UTC, datetime

from fastapi.testclient import TestClient

from nasa_agent.context import FOLLOW_UP_MARKER, is_context_message
from nasa_agent.harness import NO_ANSWER_PLACEHOLDER, NO_RECORD_REPLY, STEP_LIMIT_REPLY, TOOL_NUDGE
from nasa_agent.main import create_app
from tests.conftest import user
from tests.fakes import (
    E_CAMERA,
    E_DRILL,
    FakeInventoryBlock,
    FakeLLM,
    assistant_text,
    assistant_tools,
    tool_call,
)

TURN1_Q = "Where is the drill right now?"
TURN1_A = "The pistol-grip drill was last seen in LAB1S2 at 12:30Z."
TURN2_Q = "Who moved the camera?"
TURN2_A = "The Nikon camera was moved by crew member, red shirt at 11:00Z from LAB1P3 to NOD2D4."
# what the real model did in turn 2: answered from the earlier answer, no tool call
TURN2_UNGROUNDED = "The camera was last seen in LAB1S2 at 12:30Z."


def chat_part(msgs):
    return [m for m in msgs if m["role"] not in ("system", "tool") and not is_context_message(m)
            and not m.get("tool_calls")]


def two_turn_history(turn1_answer: str = TURN1_A) -> list[dict]:
    return [{"role": "user", "content": TURN1_Q}, {"role": "assistant", "content": turn1_answer},
            {"role": "user", "content": TURN2_Q}]


# ---------------------------------------------------------------- the P1 reproduction
async def test_ac_agt_12_follow_up_that_skips_tools_is_nudged_then_grounded_by_turn_2_tools(make_harness):
    llm = FakeLLM([
        assistant_text(TURN2_UNGROUNDED),  # step 1: copies the old answer, no tool (the live failure)
        assistant_tools(tool_call("who_moved", {"name": "camera"})),
        assistant_text(TURN2_A),
    ])
    res = await make_harness(llm).run(two_turn_history())
    assert res["reply"] == TURN2_A, "turn 2 must be answered from turn 2's tool results, not refused"
    assert [c["name"] for c in res["tool_calls"]] == ["who_moved"]
    cited = {c.get("entity_id") for c in res["citations"]}
    assert E_CAMERA in cited and E_DRILL not in cited, "citations come from this turn's tools only"
    nudge_req = llm.requests[1]["messages"]
    assert nudge_req[-2] == {"role": "assistant", "content": TURN2_UNGROUNDED}
    assert nudge_req[-1] == {"role": "user", "content": TOOL_NUDGE}


async def test_ac_agt_12_http_two_sequential_requests_turn_2_calls_tools(plain_settings, inventory, fake_memory):
    """Same flow over POST /chat, as the dashboard sends it (whole transcript)."""
    llm = FakeLLM([
        assistant_tools(tool_call("where_is", {"name": "drill"})), assistant_text(TURN1_A),   # turn 1
        assistant_text(TURN2_UNGROUNDED),                                                      # turn 2, step 1
        assistant_tools(tool_call("who_moved", {"name": "camera"})), assistant_text(TURN2_A),
    ])
    app = create_app(plain_settings, llm=llm, inventory=inventory, memory=fake_memory,
                     inventory_block=FakeInventoryBlock(), dreamer=None)
    with TestClient(app) as c:
        r1 = c.post("/chat", json={"messages": user(TURN1_Q)}).json()
        assert r1["reply"] == TURN1_A
        history = [*user(TURN1_Q), {"role": "assistant", "content": r1["reply"]}, *user(TURN2_Q)]
        r2 = c.post("/chat", json={"messages": history}).json()
    assert r2["reply"] == TURN2_A and r2["reply"] != NO_RECORD_REPLY
    assert any(cit.get("entity_id") == E_CAMERA for cit in r2["citations"])


async def test_ac_agt_12_follow_up_still_refused_when_model_never_calls_a_tool(make_harness):
    """Grounding stays per turn: restating the earlier answer is never accepted, nudge or not."""
    llm = FakeLLM([assistant_text(TURN2_UNGROUNDED), assistant_text(TURN2_UNGROUNDED)])
    res = await make_harness(llm).run(two_turn_history())
    assert res["reply"] == NO_RECORD_REPLY and res["citations"] == []
    assert llm.calls == 2, "exactly one nudge"


async def test_ac_agt_12_first_turn_is_not_nudged(make_harness):
    llm = FakeLLM([assistant_text("hello")])
    await make_harness(llm).run(user("hi"))
    assert llm.calls == 1


async def test_ac_agt_12_follow_up_that_calls_tools_needs_no_nudge(make_harness):
    llm = FakeLLM([assistant_tools(tool_call("who_moved", {"name": "camera"})), assistant_text(TURN2_A)])
    res = await make_harness(llm).run(two_turn_history())
    assert llm.calls == 2 and res["reply"] == TURN2_A
    assert all(m.get("content") != TOOL_NUDGE for req in llm.requests for m in req["messages"])


# ---------------------------------------------------------------- history preparation
async def test_ac_agt_12_latest_user_message_carries_follow_up_marker(make_harness):
    llm = FakeLLM([assistant_tools(tool_call("who_moved", {"name": "camera"})), assistant_text(TURN2_A)])
    await make_harness(llm).run(two_turn_history())
    last = chat_part(llm.requests[0]["messages"])[-1]
    assert last["role"] == "user" and last["content"].startswith(TURN2_Q)
    assert FOLLOW_UP_MARKER in last["content"]


async def test_ac_agt_12_first_turn_has_no_marker(make_harness):
    llm = FakeLLM([assistant_text("hello")])
    await make_harness(llm).run(user("hi"))
    assert chat_part(llm.requests[0]["messages"])[-1]["content"] == "hi"


async def test_ac_agt_12_earlier_canned_refusals_do_not_poison_the_context(make_harness):
    hist = [*user("who used the drill?"), {"role": "assistant", "content": NO_RECORD_REPLY},
            *user("what changed?"), {"role": "assistant", "content": STEP_LIMIT_REPLY}, *user(TURN2_Q)]
    llm = FakeLLM([assistant_tools(tool_call("who_moved", {"name": "camera"})), assistant_text(TURN2_A)])
    await make_harness(llm).run(hist)
    sent = [m["content"] for m in chat_part(llm.requests[0]["messages"]) if m["role"] == "assistant"]
    assert sent == [NO_ANSWER_PLACEHOLDER, NO_ANSWER_PLACEHOLDER]


async def test_ac_agt_12_earlier_long_answers_are_cut(make_harness, plain_settings):
    from nasa_agent.context import estimate_tokens

    long_answer = "The laptop entries are: " + "laptop `da9709e7-4ed6-4b2d-beb0-aa6c814ae764` in USOS; " * 60
    llm = FakeLLM([assistant_tools(tool_call("who_moved", {"name": "camera"})), assistant_text(TURN2_A)])
    await make_harness(llm).run(two_turn_history(long_answer))
    prior = next(m["content"] for m in chat_part(llm.requests[0]["messages"]) if m["role"] == "assistant")
    assert prior.startswith("The laptop entries are:")
    assert estimate_tokens(prior) <= plain_settings.history_reply_tokens


async def test_ac_agt_12_roles_strictly_alternate_for_strict_chat_templates(make_harness):
    """A failed request leaves two user messages in a row in the dashboard transcript (Gemma templates reject that)."""
    hist = [*user("where is the drill?"), *user("hello?"), {"role": "assistant", "content": "Hi."},
            {"role": "assistant", "content": "Anything else?"}, *user(TURN2_Q)]
    llm = FakeLLM([assistant_tools(tool_call("who_moved", {"name": "camera"})), assistant_text(TURN2_A)])
    await make_harness(llm).run(hist)
    roles = [m["role"] for m in llm.requests[0]["messages"]]
    assert roles[0] == "system"
    assert all(a != b for a, b in itertools.pairwise(roles[1:])), roles
    assert roles[-1] == "user"


async def test_ac_agt_12_data_message_carries_current_utc_time(make_harness, toolbox):
    toolbox.now = lambda: datetime(2026, 10, 18, 0, 30, tzinfo=UTC)
    llm = FakeLLM([assistant_text("hello")])
    await make_harness(llm).run(user("what changed with the drill in the last hour?"))  # not routed (AC-AGT-35)
    data = next(m["content"] for m in llm.requests[0]["messages"] if is_context_message(m) and m["role"] == "user")
    assert "Current time (UTC): 2026-10-18T00:30:00Z" in data
