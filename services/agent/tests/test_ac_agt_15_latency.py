"""AC-AGT-15: latency bounds (UI review UI-02/UI-05: 57-64 s answers on the T4 with ~125 items).

Per step the model re-reads system prompt + tool specs + data block + history + every tool result of the turn, and a
long markdown answer is decoded at ~20-40 tok/s. These tests pin the bounds with the package's token estimate."""
from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import replace

import httpx
import pytest
from fastapi.testclient import TestClient

from nasa_agent.config import Settings
from nasa_agent.context import SYSTEM_PROMPT, estimate_request_tokens, estimate_tokens
from nasa_agent.harness import STEP_LIMIT_REPLY, TIMEOUT_RECORDS_LEAD
from nasa_agent.inventory_block import InventoryBlockRenderer
from nasa_agent.llm import LLMError, LLMTimeout, OpenAICompatLLM
from nasa_agent.main import create_app
from tests.conftest import user
from tests.fakes import (
    E_DRILL,
    LLM_URL,
    FakeInventoryBlock,
    FakeLLM,
    FakeLLMServer,
    _item,
    assistant_text,
    assistant_tools,
    tool_call,
)

WHERE = tool_call("where_is", {"name": "drill"})
STATS = tool_call("inventory_stats", {})


# ---------------------------------------------------------------- defaults
def test_ac_agt_15_tighter_default_budgets():
    s = Settings(agent_llm_url="u", inventory_api_url="i", database_url="d")
    assert s.inventory_block_token_budget == 600
    assert s.tool_result_token_budget == 800
    assert s.max_output_tokens == 400
    assert s.grounded_extra_steps == 1
    assert s.turn_deadline_s == 45.0
    assert s.history_reply_tokens == 150


def test_ac_agt_15_new_settings_from_env():
    s = Settings.from_env({"AGENT_LLM_URL": "u", "INVENTORY_API_URL": "i", "DATABASE_URL": "d",
                           "AGENT_MAX_OUTPUT_TOKENS": "256", "AGENT_GROUNDED_EXTRA_STEPS": "2",
                           "AGENT_TURN_DEADLINE_S": "30", "AGENT_HISTORY_REPLY_TOKENS": "80"})
    assert (s.max_output_tokens, s.grounded_extra_steps, s.turn_deadline_s, s.history_reply_tokens) == (256, 2, 30.0, 80)


def test_ac_agt_15_grounded_extra_steps_may_be_zero():
    s = Settings.from_env({"AGENT_LLM_URL": "u", "INVENTORY_API_URL": "i", "DATABASE_URL": "d",
                           "AGENT_GROUNDED_EXTRA_STEPS": "0"})
    assert s.grounded_extra_steps == 0


async def test_ac_agt_15_llm_request_carries_max_tokens():
    server = FakeLLMServer([assistant_text("hi")])
    llm = OpenAICompatLLM(LLM_URL, "agent-llm", transport=server.transport, max_tokens=321)
    await llm.complete([{"role": "user", "content": "hi"}], [])
    assert server.bodies()[0]["max_tokens"] == 321


def test_ac_agt_15_create_app_wires_max_tokens(plain_settings, inventory, fake_memory):
    server = FakeLLMServer([assistant_text("hi")])
    app = create_app(replace(plain_settings, max_output_tokens=222), inventory=inventory, memory=fake_memory,
                     inventory_block=FakeInventoryBlock(), dreamer=None, llm_transport=server.transport)
    with TestClient(app) as c:
        c.post("/chat", json={"messages": user("hi")})
    assert server.bodies()[0]["max_tokens"] == 222


# ---------------------------------------------------------------- prompt size
def _no_titles(obj) -> bool:
    if isinstance(obj, dict):
        return "title" not in obj and all(_no_titles(v) for v in obj.values())
    if isinstance(obj, list):
        return all(_no_titles(v) for v in obj)
    return True


def test_ac_agt_15_tool_specs_are_compact(toolbox):
    specs = toolbox.specs()
    assert _no_titles([s["function"]["parameters"] for s in specs])
    assert estimate_tokens(json.dumps(specs)) <= 2000, "tool specs are re-sent on every step"


def test_ac_agt_15_system_prompt_asks_for_short_plain_answers():
    text = SYSTEM_PROMPT.lower()
    assert "short" in text and "uuid" in text
    assert estimate_tokens(SYSTEM_PROMPT) <= 650


def _big_block(budget: int) -> str:
    reviews = [f"- review {i:08d}-aaaa-4aaa-8aaa-aaaaaaaaaaaa unknown_object: (no entity) at NOD1; \"grey object "
               f"with a strap, maybe a bag\" observed 2026-10-17T22:{i % 60:02d}:00Z" for i in range(69)]
    moves = [f"- drill [{i:08d}-bbbb-4bbb-8bbb-bbbbbbbbbbbb] moved NOD1 -> LAB1 by crew member in blue shirt at "
             f"2026-10-17T22:{i % 60:02d}:00Z; now at LAB1" for i in range(40)]
    return InventoryBlockRenderer._fit([("Open reviews:", reviews), ("Moved in the last 24 h:", moves)], budget)


def test_ac_agt_15_large_inventory_block_fits_default_budget():
    s = Settings(agent_llm_url="u", inventory_api_url="i", database_url="d")
    assert estimate_tokens(_big_block(s.inventory_block_token_budget)) <= 600


async def test_ac_agt_15_typical_question_prompt_tokens_bounded(make_harness, fake_inventory, plain_settings):
    """~125 items: where_is returns a big page; 2 LLM steps. Per-step and total prompt tokens stay bounded."""
    fake_inventory.items += [
        _item(f"{i:08d}-0000-4000-8000-0000000000ff", f"laptop {i}", catalog_id="laptop", catalog_name="SSC laptop",
              last_location="NOD1", last_location_name="Node 1 Unity", last_seen_at=f"2026-10-01T12:{i % 60:02d}:00Z",
              description="grey laptop with stickers " * 4)
        for i in range(120)
    ]
    llm = FakeLLM([assistant_tools(tool_call("search_items", {"q": "laptop", "filters": {"limit": 100}})),
                   assistant_text("Several laptops are in NOD1.")])
    block = FakeInventoryBlock(_big_block(plain_settings.inventory_block_token_budget))
    await make_harness(llm, inventory_block=block).run(user("Where are the laptops?"))
    per_step = [estimate_request_tokens(r["messages"], r["tools"]) for r in llm.requests]
    assert max(per_step) <= 4200, per_step
    assert sum(per_step) <= 8000, per_step


async def test_ac_agt_15_turn_log_reports_prompt_token_estimates(make_harness, caplog):
    caplog.set_level(logging.INFO, logger="nasa_agent.harness")
    llm = FakeLLM([assistant_tools(WHERE), assistant_text("The pistol-grip drill was last seen in LAB1S2.")])
    await make_harness(llm).run(user("where is the drill?"))
    rec = next(r for r in caplog.records if r.getMessage() == "chat turn")
    assert isinstance(rec.prompt_tokens_est, list) and len(rec.prompt_tokens_est) == 2
    assert rec.prompt_tokens_total == sum(rec.prompt_tokens_est) > 0


# ---------------------------------------------------------------- early stop
async def test_ac_agt_15_early_stop_after_grounding(make_harness, fake_inventory):
    llm = FakeLLM([], default=assistant_tools(WHERE))
    res = await make_harness(llm).run(user("where is the drill?"))
    assert llm.calls == 3, "grounded at step 1, one extra tool step, then the final no-tool call"
    assert [r["tool_choice"] for r in llm.requests] == ["auto", "auto", "none"]
    assert len(fake_inventory.calls("/items")) == 2
    assert res["reply"] == STEP_LIMIT_REPLY


async def test_ac_agt_15_early_stop_final_call_answers(make_harness):
    llm = FakeLLM([assistant_tools(WHERE), assistant_tools(WHERE),
                   assistant_text("The pistol-grip drill was last seen in LAB1S2 at 12:30Z.")])
    res = await make_harness(llm).run(user("where is the drill?"))
    assert res["reply"].startswith("The pistol-grip drill") and llm.requests[2]["tool_choice"] == "none"


async def test_ac_agt_15_zero_extra_steps_forces_answer_right_after_grounding(make_harness, plain_settings):
    llm = FakeLLM([assistant_tools(WHERE), assistant_text("The pistol-grip drill was last seen in LAB1S2.")])
    await make_harness(llm, settings=replace(plain_settings, grounded_extra_steps=0)).run(user("drill?"))
    assert [r["tool_choice"] for r in llm.requests] == ["auto", "none"]


async def test_ac_agt_15_ungrounded_loop_still_capped_at_six_with_final_no_tool_call(make_harness):
    llm = FakeLLM([], default=assistant_tools(STATS))
    await make_harness(llm).run(user("stats forever"))
    assert llm.calls == 6
    assert llm.requests[-1]["tool_choice"] == "none"
    assert all(r["tool_choice"] == "auto" for r in llm.requests[:-1])


# ---------------------------------------------------------------- deadline / timeouts
class SleepyLLM(FakeLLM):
    """Sleeps ``delays[i]`` seconds before answering call i; records cancellation."""

    def __init__(self, script, delays):
        super().__init__(script)
        self.delays = list(delays)
        self.cancelled = 0

    async def complete(self, messages, tools, tool_choice="auto"):
        delay = self.delays.pop(0) if self.delays else 0
        try:
            await asyncio.sleep(delay)
        except asyncio.CancelledError:
            self.cancelled += 1
            raise
        return await super().complete(messages, tools, tool_choice)


async def test_ac_agt_15_deadline_without_data_raises_llm_timeout(make_harness, plain_settings):
    llm = SleepyLLM([assistant_text("late")], [5])
    with pytest.raises(LLMTimeout):
        await make_harness(llm, settings=replace(plain_settings, turn_deadline_s=0.2)).run(user("drill?"))
    assert llm.cancelled == 1, "the in-flight LLM call is cancelled, not left running"


async def test_ac_agt_15_deadline_after_grounding_returns_honest_records_list(make_harness, plain_settings):
    llm = SleepyLLM([assistant_tools(WHERE), assistant_text("late")], [0, 5])
    res = await make_harness(llm, settings=replace(plain_settings, turn_deadline_s=0.3)).run(user("drill?"))
    reply = res["reply"]
    assert reply.startswith(TIMEOUT_RECORDS_LEAD)
    assert "pistol-grip drill" in reply and "LAB1S2" in reply and "12:30 UTC" in reply
    assert any(c.get("entity_id") == E_DRILL for c in res["citations"])


def test_ac_agt_15_chat_timeout_is_504_with_retry_hint(plain_settings, inventory, fake_memory):
    app = create_app(replace(plain_settings, turn_deadline_s=0.2), llm=SleepyLLM([assistant_text("x")], [5]),
                     inventory=inventory, memory=fake_memory, inventory_block=FakeInventoryBlock(), dreamer=None)
    with TestClient(app) as c:
        r = c.post("/chat", json={"messages": user("what changed?")})
    assert r.status_code == 504
    body = r.json()
    assert body["kind"] == "timeout" and body["retryable"] is True and body["request_id"]
    assert "try again" in body["detail"].lower() or "retry" in body["detail"].lower()
    assert "llm failure" not in body["detail"].lower()


def test_ac_agt_15_chat_llm_down_is_502_with_human_detail(plain_settings, inventory, fake_memory):
    server = FakeLLMServer([], status=500)
    app = create_app(plain_settings, llm=OpenAICompatLLM(LLM_URL, "m", transport=server.transport),
                     inventory=inventory, memory=fake_memory, inventory_block=FakeInventoryBlock(), dreamer=None)
    with TestClient(app) as c:
        r = c.post("/chat", json={"messages": user("drill?")})
    assert r.status_code == 502
    assert r.json()["kind"] == "llm" and r.json()["retryable"] is True


async def test_ac_agt_15_http_timeout_maps_to_llm_timeout():
    def handler(request):
        raise httpx.ReadTimeout("slow", request=request)

    llm = OpenAICompatLLM(LLM_URL, "m", transport=httpx.MockTransport(handler))
    with pytest.raises(LLMTimeout) as exc:
        await llm.complete([{"role": "user", "content": "x"}], [])
    assert isinstance(exc.value, LLMError)
