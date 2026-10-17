"""AC-AGT-3 + conventions ("model outputs are untrusted"): every assistant message from the LLM is validated before
use (review round 1). Structurally malformed messages are an ``LLMError`` (HTTP 502 with request_id), never a 500.
OpenAI content-part lists are joined into text; tool calls without an id get a synthetic id that is used both in the
echoed assistant message and in the matching tool message.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from nasa_agent.llm import LLMError
from nasa_agent.main import create_app
from tests.conftest import user
from tests.fakes import FakeInventoryBlock, FakeLLM, assistant_text, assistant_tools, tool_call

MALFORMED = [
    {"role": "assistant", "content": None, "tool_calls": ["where_is"]},
    {"role": "assistant", "content": None, "tool_calls": [{"id": "x", "type": "function", "function": "where_is"}]},
    {"role": "assistant", "content": None, "tool_calls": {"id": "x"}},
    {"role": "assistant", "content": None, "tool_calls": [{"id": "x", "type": "function", "function": {"arguments": "{}"}}]},
    {"role": "assistant", "content": {"text": "hi"}},
    {"role": "assistant", "content": 42},
]


@pytest.mark.parametrize("message", MALFORMED)
async def test_ac_agt_03_malformed_assistant_message_is_llm_error(make_harness, message):
    with pytest.raises(LLMError):
        await make_harness(FakeLLM([message])).run(user("where is the drill?"))


@pytest.mark.parametrize("message", MALFORMED)
def test_ac_agt_03_malformed_assistant_message_is_502_with_request_id(plain_settings, inventory, fake_memory, message):
    app = create_app(plain_settings, llm=FakeLLM([message]), inventory=inventory, memory=fake_memory,
                     inventory_block=FakeInventoryBlock(), dreamer=None)
    with TestClient(app, raise_server_exceptions=False) as c:
        r = c.post("/chat", json={"messages": user("where is the drill?")})
    assert r.status_code == 502, r.text
    assert r.json()["request_id"] == r.headers["x-request-id"]


def test_ac_agt_03_unexpected_error_is_500_with_request_id(plain_settings, inventory, fake_memory):
    class Boom:
        async def complete(self, messages, tools, tool_choice="auto"):
            raise RuntimeError("unexpected")

    app = create_app(plain_settings, llm=Boom(), inventory=inventory, memory=fake_memory,
                     inventory_block=FakeInventoryBlock(), dreamer=None)
    with TestClient(app, raise_server_exceptions=False) as c:
        r = c.post("/chat", json={"messages": user("hi")})
    assert r.status_code == 500
    assert r.json()["request_id"] == r.headers["x-request-id"]
    assert "unexpected" not in r.text


async def test_ac_agt_03_content_part_list_is_joined(make_harness):
    llm = FakeLLM([
        assistant_tools(tool_call("where_is", {"name": "drill"})),
        {"role": "assistant", "content": [{"type": "text", "text": "The drill is "}, {"type": "text", "text": "in LAB1S2."}]},
    ])
    res = await make_harness(llm).run(user("where is the drill?"))
    assert res["reply"] == "The drill is in LAB1S2."


async def test_ac_agt_03_missing_tool_call_id_is_consistent(make_harness):
    call = {"type": "function", "function": {"name": "where_is", "arguments": '{"name": "drill"}'}}
    llm = FakeLLM([{"role": "assistant", "content": None, "tool_calls": [call]}, assistant_text("LAB1S2.")])
    await make_harness(llm).run(user("where is the drill?"))
    msgs = llm.requests[1]["messages"]
    asst = [m for m in msgs if m.get("role") == "assistant" and m.get("tool_calls")][-1]
    tool = [m for m in msgs if m.get("role") == "tool"][-1]
    assert asst["tool_calls"][0]["id"] and asst["tool_calls"][0]["id"] == tool["tool_call_id"]


async def test_ac_agt_03_dict_arguments_accepted(make_harness):
    call = {"id": "d", "type": "function", "function": {"name": "where_is", "arguments": {"name": "drill"}}}
    llm = FakeLLM([{"role": "assistant", "content": None, "tool_calls": [call]}, assistant_text("LAB1S2.")])
    res = await make_harness(llm).run(user("where is the drill?"))
    assert res["tool_calls"][0]["ok"] is True
