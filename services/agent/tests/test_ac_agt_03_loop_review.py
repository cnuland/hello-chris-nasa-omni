"""AC-AGT-3 loop details from review round 1.

- Invalid calls are counted per LLM step: every invalid call of the first step that has one is fed back (and the valid
  calls of that step still run); the loop only stops on a later step that again contains an invalid call.
- Tools requested in the last allowed step are not executed (their results could never reach the model).
- An empty final answer in a grounded turn gets its own reply, not the step-limit text.
- Tool payloads fed back to the model are capped (``Settings.tool_result_token_budget``) and stay valid JSON.
- Chat message content is capped at ``MAX_MESSAGE_CHARS``; the whole request at ``MAX_REQUEST_CHARS``.
"""
from __future__ import annotations

import json
from dataclasses import replace

from fastapi.testclient import TestClient

from nasa_agent.context import estimate_tokens
from nasa_agent.harness import EMPTY_REPLY, STEP_LIMIT_REPLY
from nasa_agent.main import MAX_MESSAGE_CHARS, MAX_REQUEST_CHARS, create_app
from tests.conftest import user
from tests.fakes import FakeInventoryBlock, FakeLLM, _item, assistant_text, assistant_tools, tool_call


def tool_messages(request: dict) -> list[dict]:
    return [m for m in request["messages"] if m.get("role") == "tool"]


async def test_ac_agt_03_two_invalid_calls_in_one_step_are_both_fed_back(make_harness):
    llm = FakeLLM([
        assistant_tools(tool_call("where_is", {}, "bad1"), tool_call("get_item", {"entity_id": "x"}, "bad2"),
                        tool_call("where_is", {"name": "drill"}, "good")),
        assistant_text("The drill is in LAB1S2."),
    ])
    res = await make_harness(llm).run(user("where is the drill?"))
    assert llm.calls == 2
    fed = {t["tool_call_id"]: t["content"] for t in tool_messages(llm.requests[1])}
    assert set(fed) == {"bad1", "bad2", "good"}
    assert "error" in fed["bad1"].lower() and "error" in fed["bad2"].lower()
    assert res["reply"] == "The drill is in LAB1S2."


async def test_ac_agt_03_second_invalid_step_stops_without_running_its_valid_calls(make_harness, fake_inventory):
    llm = FakeLLM([
        assistant_tools(tool_call("where_is", {}, "bad1")),
        assistant_tools(tool_call("where_is", {"name": ""}, "bad2"), tool_call("where_is", {"name": "drill"}, "ok")),
        assistant_text("never"),
    ])
    await make_harness(llm).run(user("where is the drill?"))
    assert llm.calls == 2
    assert fake_inventory.requests == []


async def test_ac_agt_03_tools_of_last_step_are_not_executed(make_harness, plain_settings, fake_inventory):
    llm = FakeLLM([], default=assistant_tools(tool_call("where_is", {"name": "drill"})))
    res = await make_harness(llm, settings=replace(plain_settings, max_steps=3)).run(user("loop"))
    assert llm.calls == 3
    assert len(fake_inventory.calls("/items")) == 2, "only the first two steps' tools can reach the model"
    assert res["reply"] == STEP_LIMIT_REPLY


async def test_ac_agt_03_empty_final_answer_is_not_called_a_step_limit(make_harness):
    llm = FakeLLM([assistant_tools(tool_call("where_is", {"name": "drill"})), assistant_text("   ")])
    res = await make_harness(llm).run(user("where is the drill?"))
    assert res["reply"] == EMPTY_REPLY
    assert res["reply"] != STEP_LIMIT_REPLY


async def test_ac_agt_03_large_tool_payload_is_capped_and_valid_json(make_harness, plain_settings, fake_inventory):
    fake_inventory.items = [
        _item(f"{i:08d}-0000-4000-8000-000000000000", f"drill bit {i}", description="long text " * 60,
              last_location="LAB1S2", last_seen_at="2026-10-01T12:00:00Z")
        for i in range(100)
    ]
    settings = replace(plain_settings, tool_result_token_budget=800)
    llm = FakeLLM([assistant_tools(tool_call("search_items", {"q": "drill", "filters": {"limit": 100}})),
                   assistant_text("Many drill bits in LAB1S2.")])
    await make_harness(llm, settings=settings).run(user("list drill bits"))
    content = tool_messages(llm.requests[1])[0]["content"]
    assert estimate_tokens(content) <= 800
    payload = json.loads(content)
    assert payload["ok"] is True and payload.get("truncated") is True
    assert payload["data"], "as many whole items as fit are kept"


def _client(plain_settings, inventory, fake_memory, llm):
    app = create_app(plain_settings, llm=llm, inventory=inventory, memory=fake_memory,
                     inventory_block=FakeInventoryBlock(), dreamer=None)
    return TestClient(app, raise_server_exceptions=False)


def test_ac_agt_03_oversized_message_is_422(plain_settings, inventory, fake_memory):
    llm = FakeLLM([assistant_text("never")])
    with _client(plain_settings, inventory, fake_memory, llm) as c:
        r = c.post("/chat", json={"messages": user("x" * (MAX_MESSAGE_CHARS + 1))})
    assert r.status_code == 422 and llm.calls == 0


def test_ac_agt_03_oversized_request_is_422(plain_settings, inventory, fake_memory):
    llm = FakeLLM([assistant_text("never")])
    n = MAX_REQUEST_CHARS // MAX_MESSAGE_CHARS + 2
    msgs = []
    for _ in range(n):
        msgs += [{"role": "user", "content": "q" * MAX_MESSAGE_CHARS}, {"role": "assistant", "content": "a"}]
    msgs.append({"role": "user", "content": "q"})
    with _client(plain_settings, inventory, fake_memory, llm) as c:
        r = c.post("/chat", json={"messages": msgs})
    assert r.status_code == 422 and llm.calls == 0
