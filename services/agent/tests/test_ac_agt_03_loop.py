"""AC-AGT-3: tool-calling loop against an OpenAI-compatible LLM (AGENT_LLM_URL, tools, tool_choice=auto), at most 6
steps; tool arguments validated with pydantic and invalid args fed back as a tool error once.

A "step" is one LLM call. ``AgentHarness.run`` returns {request_id, session_id, reply, citations, tool_calls,
proposals}.
"""
from __future__ import annotations

import json

import pytest

from nasa_agent.config import Settings
from nasa_agent.context import is_context_message
from nasa_agent.llm import LLMError, OpenAICompatLLM
from tests.conftest import user
from tests.fakes import (
    E_CAMERA,
    E_DRILL,
    INVENTORY_URL,
    LLM_URL,
    FakeLLM,
    FakeLLMServer,
    assistant_text,
    assistant_tools,
    tool_call,
)

WHERE_DRILL = tool_call("where_is", {"name": "drill"}, "c1")


def tool_messages(request: dict) -> list[dict]:
    return [m for m in request["messages"] if m.get("role") == "tool"]


# ---------------------------------------------------------------- loop mechanics
async def test_ac_agt_03_every_llm_request_sends_tools_and_tool_choice_auto(make_harness, toolbox):
    llm = FakeLLM([assistant_tools(WHERE_DRILL), assistant_text("The drill is in LAB1S2.")])
    await make_harness(llm).run(user("where is the drill?"))
    assert llm.calls == 2
    expected = {s["function"]["name"] for s in toolbox.specs()}
    for req in llm.requests:
        assert req["tool_choice"] == "auto"
        assert {t["function"]["name"] for t in req["tools"]} == expected


async def test_ac_agt_03_tool_result_is_fed_back_as_tool_message(make_harness):
    llm = FakeLLM([assistant_tools(WHERE_DRILL), assistant_text("The drill is in LAB1S2.")])
    res = await make_harness(llm).run(user("where is the drill?"))
    second = llm.requests[1]["messages"]
    asst = [m for m in second if m.get("role") == "assistant" and m.get("tool_calls")]
    assert asst and asst[-1]["tool_calls"][0]["id"] == "c1"
    tools = tool_messages(llm.requests[1])
    assert len(tools) == 1 and tools[0]["tool_call_id"] == "c1"
    payload = json.loads(tools[0]["content"])
    assert E_DRILL in json.dumps(payload) and "LAB1S2" in json.dumps(payload)
    assert res["reply"] == "The drill is in LAB1S2."


async def test_ac_agt_03_conversation_order_preserved_user_last_before_tools(make_harness):
    llm = FakeLLM([assistant_text("ok")])
    history = [
        {"role": "user", "content": "first question"},
        {"role": "assistant", "content": "first answer"},
        {"role": "user", "content": "where is the drill?"},
    ]
    await make_harness(llm).run(history)
    chat = [m for m in llm.requests[0]["messages"]
            if m["role"] in ("user", "assistant") and not is_context_message(m)]
    assert [m["content"] for m in chat] == ["first question", "first answer", "where is the drill?"]
    assert llm.requests[0]["messages"][0]["role"] == "system"


async def test_ac_agt_03_parallel_tool_calls_in_one_step_all_execute(make_harness):
    llm = FakeLLM([
        assistant_tools(tool_call("where_is", {"name": "drill"}, "a"), tool_call("who_moved", {"name": "camera"}, "b")),
        assistant_text("Drill in LAB1S2; camera moved by crew member, red shirt."),
    ])
    res = await make_harness(llm).run(user("drill and camera?"))
    tools = tool_messages(llm.requests[1])
    assert {t["tool_call_id"] for t in tools} == {"a", "b"}
    assert [c["name"] for c in res["tool_calls"]] == ["where_is", "who_moved"]
    assert llm.calls == 2


async def test_ac_agt_03_tool_calls_are_reported_with_name_arguments_and_status(make_harness):
    llm = FakeLLM([assistant_tools(WHERE_DRILL), assistant_text("The drill is in LAB1S2.")])
    res = await make_harness(llm).run(user("where is the drill?"))
    assert len(res["tool_calls"]) == 1
    tc = res["tool_calls"][0]
    assert tc["name"] == "where_is"
    args = tc["arguments"] if isinstance(tc["arguments"], dict) else json.loads(tc["arguments"])
    assert args == {"name": "drill"}
    assert tc["ok"] is True


async def test_ac_agt_03_at_most_six_steps_when_llm_never_stops(make_harness):
    llm = FakeLLM([], default=assistant_tools(WHERE_DRILL))
    res = await make_harness(llm).run(user("loop forever"))
    assert llm.calls == 6
    assert isinstance(res["reply"], str) and res["reply"].strip()
    assert len(res["tool_calls"]) <= 6


async def test_ac_agt_03_configured_max_steps_lower_than_six_is_respected(make_harness, plain_settings):
    from dataclasses import replace

    llm = FakeLLM([], default=assistant_tools(WHERE_DRILL))
    await make_harness(llm, settings=replace(plain_settings, max_steps=3)).run(user("loop"))
    assert llm.calls == 3


async def test_ac_agt_03_max_steps_above_six_is_still_capped(make_harness, plain_settings):
    from dataclasses import replace

    llm = FakeLLM([], default=assistant_tools(WHERE_DRILL))
    await make_harness(llm, settings=replace(plain_settings, max_steps=50)).run(user("loop"))
    assert llm.calls == 6


async def test_ac_agt_03_final_answer_on_first_step_makes_one_call(make_harness, fake_inventory):
    llm = FakeLLM([assistant_text("hello")])
    await make_harness(llm).run(user("hi"))
    assert llm.calls == 1
    assert fake_inventory.requests == []


# ---------------------------------------------------------------- argument validation
async def test_ac_agt_03_invalid_args_fed_back_as_tool_error_then_model_recovers(make_harness, fake_inventory):
    llm = FakeLLM([
        assistant_tools(tool_call("where_is", {"nam": "drill"}, "bad")),
        assistant_tools(tool_call("where_is", {"name": "drill"}, "good")),
        assistant_text("The drill is in LAB1S2."),
    ])
    res = await make_harness(llm).run(user("where is the drill?"))
    assert llm.calls == 3
    err = [t for t in tool_messages(llm.requests[1]) if t["tool_call_id"] == "bad"]
    assert len(err) == 1
    content = err[0]["content"].lower()
    assert "error" in content and "name" in content
    assert res["reply"] == "The drill is in LAB1S2."
    assert [c["ok"] for c in res["tool_calls"]] == [False, True]


async def test_ac_agt_03_invalid_args_fed_back_only_once(make_harness, fake_inventory):
    llm = FakeLLM([
        assistant_tools(tool_call("where_is", {}, "bad1")),
        assistant_tools(tool_call("where_is", {"name": ""}, "bad2")),
        assistant_text("The drill is in LAB1S2."),
    ])
    res = await make_harness(llm).run(user("where is the drill?"))
    assert llm.calls == 2, "a second invalid tool call must not be fed back again"
    assert fake_inventory.requests == []
    assert "LAB1S2" not in res["reply"]


async def test_ac_agt_03_malformed_json_arguments_are_a_validation_error(make_harness, fake_inventory):
    llm = FakeLLM([
        assistant_tools(tool_call("where_is", '{"name": "drill"', "bad")),
        assistant_tools(tool_call("where_is", {"name": "drill"}, "good")),
        assistant_text("The drill is in LAB1S2."),
    ])
    res = await make_harness(llm).run(user("where is the drill?"))
    assert llm.calls == 3
    assert "error" in tool_messages(llm.requests[1])[0]["content"].lower()
    assert res["reply"] == "The drill is in LAB1S2."


async def test_ac_agt_03_unknown_tool_is_fed_back_as_error(make_harness, fake_inventory):
    llm = FakeLLM([
        assistant_tools(tool_call("execute_sql", {"sql": "DELETE FROM entity"}, "x")),
        assistant_tools(tool_call("who_moved", {"name": "camera"}, "y")),
        assistant_text("The camera was moved by crew member, red shirt."),
    ])
    res = await make_harness(llm).run(user("who moved the camera?"))
    assert llm.calls == 3
    assert "error" in tool_messages(llm.requests[1])[0]["content"].lower()
    assert fake_inventory.methods <= {"GET"}
    assert any(c.get("entity_id") == E_CAMERA for c in res["citations"])


# ---------------------------------------------------------------- OpenAI-compatible client (wire format)
async def test_ac_agt_03_openai_client_posts_chat_completions_with_tools_auto():
    server = FakeLLMServer([assistant_tools(WHERE_DRILL)])
    llm = OpenAICompatLLM(LLM_URL, "qwen3-8b", transport=server.transport)
    tools = [{"type": "function", "function": {"name": "where_is", "description": "d",
                                               "parameters": {"type": "object", "properties": {}}}}]
    msg = await llm.complete([{"role": "user", "content": "hi"}], tools, tool_choice="auto")
    req = server.requests[0]
    assert req.method == "POST" and str(req.url) == f"{LLM_URL}/chat/completions"
    body = server.bodies()[0]
    assert body["model"] == "qwen3-8b"
    assert body["tool_choice"] == "auto"
    assert body["tools"] == tools
    assert body["messages"] == [{"role": "user", "content": "hi"}]
    assert msg["tool_calls"][0]["function"]["name"] == "where_is"


async def test_ac_agt_03_openai_client_sends_bearer_only_when_key_configured():
    server = FakeLLMServer([assistant_text("a"), assistant_text("b")])
    await OpenAICompatLLM(LLM_URL, "m", transport=server.transport).complete([{"role": "user", "content": "x"}], [])
    await OpenAICompatLLM(LLM_URL, "m", api_key="k-test", transport=server.transport).complete(
        [{"role": "user", "content": "x"}], [])
    assert "authorization" not in server.requests[0].headers
    assert server.requests[1].headers["authorization"] == "Bearer k-test"


async def test_ac_agt_03_openai_client_raises_llm_error_on_http_failure():
    server = FakeLLMServer([], status=500)
    with pytest.raises(LLMError):
        await OpenAICompatLLM(LLM_URL, "m", transport=server.transport).complete([{"role": "user", "content": "x"}], [])


async def test_ac_agt_03_harness_over_real_openai_client_and_fake_server(toolbox, fake_memory):
    from nasa_agent.harness import AgentHarness
    from tests.fakes import FakeInventoryBlock

    server = FakeLLMServer([assistant_tools(WHERE_DRILL), assistant_text("The drill is in LAB1S2.")])
    settings = Settings(agent_llm_url=LLM_URL, inventory_api_url=INVENTORY_URL, database_url="postgresql://unused")
    harness = AgentHarness(OpenAICompatLLM(LLM_URL, "agent-llm", transport=server.transport), toolbox,
                           settings=settings, memory=fake_memory, inventory_block=FakeInventoryBlock())
    res = await harness.run(user("where is the drill?"))
    assert res["reply"] == "The drill is in LAB1S2."
    bodies = server.bodies()
    assert len(bodies) == 2
    assert all(b["tool_choice"] == "auto" and b["tools"] for b in bodies)
    assert any(m.get("role") == "tool" for m in bodies[1]["messages"])
