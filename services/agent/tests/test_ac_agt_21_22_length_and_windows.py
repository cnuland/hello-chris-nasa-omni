"""AC-AGT-21 (UI2-03: answers cut mid-word at the 400-token cap, no notice) and AC-AGT-22 (UI2-04: "the last hour"
computed by the LLM, once as a 2 h window; one call returned non-JSON)."""
from __future__ import annotations

from datetime import UTC, datetime

import httpx
import pytest

from nasa_agent.context import SYSTEM_PROMPT
from nasa_agent.harness import INVALID_TOOL_REPLY, NO_RECORD_REPLY, parse_time_window
from nasa_agent.llm import LLMError, OpenAICompatLLM
from tests.conftest import user
from tests.fakes import LLM_URL, FakeLLM, assistant_text, assistant_tools, tool_call
from tests.live_round2 import DRILL_USED, LIVE_LAPTOP_CUT, NOW, load_live


def _length(text: str) -> dict:
    return {"role": "assistant", "content": text, "finish_reason": "length"}


# ---------------------------------------------------------------- AC-AGT-21
def test_ac_agt_21_prompt_plans_list_length():
    assert "at most 5 items" in SYSTEM_PROMPT
    assert "one line each" in SYSTEM_PROMPT


async def test_ac_agt_21_llm_client_passes_finish_reason():
    def handler(request):
        return httpx.Response(200, json={"choices": [{"index": 0, "finish_reason": "length",
                                                      "message": {"role": "assistant", "content": "cut"}}]})

    llm = OpenAICompatLLM(LLM_URL, "m", transport=httpx.MockTransport(handler))
    msg = await llm.complete([{"role": "user", "content": "q"}], [])
    assert msg["finish_reason"] == "length" and msg["content"] == "cut"


async def test_ac_agt_21_cut_list_is_trimmed_and_announced(make_harness, fake_inventory, toolbox):
    load_live(fake_inventory, toolbox)
    llm = FakeLLM([assistant_tools(tool_call("where_is", {"name": "laptop"})), _length(LIVE_LAPTOP_CUT)])
    r = await make_harness(llm).run(user("Where is the laptop right now?"))
    reply = r["reply"]
    assert "**Conf" not in reply and not reply.rstrip().endswith("**")
    assert reply.endswith("(showing the first 3 of 5; ask for more)")
    assert reply.count("Node 3 Tranquility") == 1, "the last complete item is kept"
    assert len(r["citations"]) == 5


async def test_ac_agt_21_cut_prose_ends_at_a_sentence(make_harness, fake_inventory, toolbox):
    load_live(fake_inventory, toolbox)
    text = "The laptop was last seen in USOS at 22:51 UTC. Another laptop was seen in NOD1 at 23:00 UTC. A third lap"
    llm = FakeLLM([assistant_tools(tool_call("where_is", {"name": "laptop"})), _length(text)])
    r = await make_harness(llm).run(user("Where is the laptop right now?"))
    assert r["reply"].endswith("at 23:00 UTC. (answer shortened; ask for more)")


async def test_ac_agt_21_complete_answer_is_untouched(make_harness, fake_inventory, toolbox):
    load_live(fake_inventory, toolbox)
    text = "The laptop was last seen in USOS at 22:51 UTC."
    llm = FakeLLM([assistant_tools(tool_call("where_is", {"name": "laptop"})), assistant_text(text)])
    r = await make_harness(llm).run(user("Where is the laptop right now?"))
    assert r["reply"] == text


# ---------------------------------------------------------------- AC-AGT-22: parsing
def _z(text: str) -> datetime:
    return datetime.fromisoformat(text)


@pytest.mark.parametrize(("text", "since", "until"), [
    ("What changed in the last hour?", "2026-10-18T00:20:00Z", None),
    ("what changed in the past hour", "2026-10-18T00:20:00Z", None),
    ("Anything in the past 30 minutes?", "2026-10-18T00:50:00Z", None),
    ("last 45 mins", "2026-10-18T00:35:00Z", None),
    ("What happened in the last 2 hours?", "2026-10-17T23:20:00Z", None),
    ("changes in the last half hour", "2026-10-18T00:50:00Z", None),
    ("in the past half an hour", "2026-10-18T00:50:00Z", None),
    ("in the last 3 days", "2026-10-15T01:20:00Z", None),
    ("What changed today?", "2026-10-18T00:00:00Z", None),
    ("What changed yesterday?", "2026-10-17T00:00:00Z", "2026-10-18T00:00:00Z"),
    ("What changed since 00:30?", "2026-10-18T00:30:00Z", None),
    ("What changed since 10:00?", "2026-10-17T10:00:00Z", None),
    ("anything since 9am", "2026-10-17T09:00:00Z", None),
    ("since 1 am", "2026-10-18T01:00:00Z", None),
    ("since 12:15 UTC", "2026-10-17T12:15:00Z", None),
])
def test_ac_agt_22_parse_time_window(text, since, until):
    window = parse_time_window(text, NOW)
    assert window is not None, text
    assert window[0] == _z(since)
    assert window[1] == (_z(until) if until else None)


@pytest.mark.parametrize("text", ["Where is the laptop right now?", "Who used the drill?",
                                  "What changed since the last review?", "the hourly report", "since 25:00"])
def test_ac_agt_22_no_window(text):
    assert parse_time_window(text, NOW) is None


# ---------------------------------------------------------------- AC-AGT-22: the harness overrides the model
async def test_ac_agt_22_model_since_is_overridden(make_harness, fake_inventory, toolbox):
    load_live(fake_inventory, toolbox)
    llm = FakeLLM([assistant_tools(tool_call("recent_changes", {"since": "2026-10-17T23:20:16Z"})),
                   assistant_text("No changes.")])
    r = await make_harness(llm).run(user("What changed in the last hour?"))
    assert r["tool_calls"][0]["arguments"]["since"] == "2026-10-18T00:20:00Z"
    p = fake_inventory.params(fake_inventory.calls("/events")[-1])
    assert p["after"] == ["2026-10-18T00:20:00Z"]


async def test_ac_agt_22_missing_since_is_filled(make_harness, fake_inventory, toolbox):
    load_live(fake_inventory, toolbox)
    llm = FakeLLM([assistant_tools(tool_call("recent_changes", {})), assistant_text("No changes.")])
    r = await make_harness(llm).run(user("What changed in the past 30 minutes?"))
    assert r["tool_calls"][0]["arguments"]["since"] == "2026-10-18T00:50:00Z"


async def test_ac_agt_22_without_phrase_model_since_stands(make_harness, fake_inventory, toolbox):
    load_live(fake_inventory, toolbox)
    llm = FakeLLM([assistant_tools(tool_call("recent_changes", {"since": "2026-10-17T22:00:00Z"})),
                   assistant_text("No changes.")])
    r = await make_harness(llm).run(user("What changed since the start of the shift?"))
    assert r["tool_calls"][0]["arguments"]["since"] == "2026-10-17T22:00:00Z"


async def test_ac_agt_22_yesterday_sets_until(make_harness, fake_inventory, toolbox):
    load_live(fake_inventory, toolbox)
    llm = FakeLLM([assistant_tools(tool_call("recent_changes", {})), assistant_text("ok")])
    r = await make_harness(llm).run(user("What changed yesterday?"))
    args = r["tool_calls"][0]["arguments"]
    assert args["since"] == "2026-10-17T00:00:00Z" and args["until"] == "2026-10-18T00:00:00Z"
    p = fake_inventory.params(fake_inventory.calls("/events")[-1])
    assert p["before"] == ["2026-10-18T00:00:00Z"]


async def test_ac_agt_22_who_moved_since_is_overridden(make_harness, fake_inventory, toolbox):
    load_live(fake_inventory, toolbox)
    llm = FakeLLM([assistant_tools(tool_call("who_moved", {"name": "drill", "since": "2026-10-16T00:00:00Z"})),
                   assistant_text("ok")])
    r = await make_harness(llm).run(user("Who used the drill in the last 3 hours?"))
    assert r["tool_calls"][0]["arguments"]["since"] == "2026-10-17T22:20:00Z"
    p = fake_inventory.params(fake_inventory.calls("/events")[-1])
    assert p["after"] == ["2026-10-17T22:20:00+00:00"] or p["after"] == ["2026-10-17T22:20:00Z"]


async def test_ac_agt_22_recent_changes_until_tool_argument(toolbox, fake_inventory):
    load_live(fake_inventory, toolbox)
    r = await toolbox.call("recent_changes", {"since": "2026-10-17T22:00:00Z", "until": "2026-10-17T22:50:00Z"})
    assert r.ok
    assert r.data["until"] == "2026-10-17T22:50:00Z"
    assert all(c["at"] < "2026-10-17T22:50:00Z" for c in r.data["changes"])
    assert not any(c.get("entity_id") == DRILL_USED for c in r.data["changes"]), "22:53 is after until"
    bad = await toolbox.call("recent_changes", {"since": "2026-10-17T22:00:00Z", "until": "2026-10-17T21:00:00Z"})
    assert not bad.ok and bad.error_kind == "validation"


# ---------------------------------------------------------------- AC-AGT-22: malformed tool-call output
TEXT_CALL = '{"name": "who_moved", "arguments": {"name": "drill", "event_types": ["used"]}}'


async def test_ac_agt_22_text_tool_call_is_run(make_harness, fake_inventory, toolbox):
    load_live(fake_inventory, toolbox)
    for content in (TEXT_CALL, f"<tool_call>\n{TEXT_CALL}\n</tool_call>"):
        fake_inventory.requests.clear()
        llm = FakeLLM([assistant_text(content), assistant_text("The drill was used by a person in blue shirt.")])
        r = await make_harness(llm).run(user("Who used the drill?"))
        assert [c["name"] for c in r["tool_calls"]] == ["who_moved"]
        assert "{" not in r["reply"] and "blue shirt" in r["reply"]
        assert fake_inventory.calls("/events")


async def test_ac_agt_22_text_tool_call_on_final_step_is_never_shown(make_harness, fake_inventory, toolbox):
    load_live(fake_inventory, toolbox)
    llm = FakeLLM([assistant_tools(tool_call("who_moved", {"name": "drill", "event_types": ["used"]})),
                   assistant_text(TEXT_CALL)], default=assistant_text(TEXT_CALL))
    r = await make_harness(llm).run(user("Who used the drill?"))
    assert "{" not in r["reply"] and "Drill used" in r["reply"]


async def test_ac_agt_22_text_tool_call_without_data_is_invalid_reply(make_harness):
    llm = FakeLLM([], default=assistant_text('{"name": "list_unknowns", "arguments": {"bogus": 7}}'))
    r = await make_harness(llm).run(user("What unknown objects are there?"))
    assert r["reply"] == INVALID_TOOL_REPLY


async def test_ac_agt_22_malformed_message_is_retried_once(make_harness, fake_inventory, toolbox):
    load_live(fake_inventory, toolbox)
    llm = FakeLLM([{"role": "assistant", "content": 5},
                   assistant_tools(tool_call("who_moved", {"name": "drill", "event_types": ["used"]})),
                   assistant_text("The drill was used by a person in blue shirt at 22:53 UTC.")])
    r = await make_harness(llm).run(user("Who used the drill?"))
    assert r["reply"] != NO_RECORD_REPLY and llm.calls == 3


async def test_ac_agt_22_second_malformed_message_is_a_clear_llm_error(make_harness):
    llm = FakeLLM([{"role": "assistant", "content": 5}, {"role": "assistant", "content": 6}])
    with pytest.raises(LLMError):
        await make_harness(llm).run(user("Who used the drill?"))
    assert llm.calls == 2


def test_ac_agt_22_chat_returns_json_error_for_malformed_llm(plain_settings, inventory, fake_memory):
    from fastapi.testclient import TestClient

    from nasa_agent.main import create_app
    from tests.fakes import FakeInventoryBlock

    llm = FakeLLM([{"role": "assistant", "content": 5}, {"role": "assistant", "content": 6}])
    app = create_app(plain_settings, llm=llm, inventory=inventory, memory=fake_memory,
                     inventory_block=FakeInventoryBlock(), dreamer=None)
    with TestClient(app) as c:
        resp = c.post("/chat", json={"messages": user("q")})
    assert resp.status_code == 502
    body = resp.json()
    assert body["kind"] == "llm" and body["retryable"] is True and body["detail"]


def test_ac_agt_22_now_is_utc():
    assert NOW.tzinfo is UTC
