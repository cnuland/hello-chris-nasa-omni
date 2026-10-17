"""AC-AGT-7: context builder = system prompt (crew privacy: role/appearance only) + global memory summary, within a
configurable token budget, oldest chat turns dropped first.

``build_context`` puts the system prompt, the inventory block (AC-AGT-10) and the global summary in system message(s)
in that order, followed by a suffix of the chat history. ``estimate_tokens`` is the single budget measure.
"""
from __future__ import annotations

from dataclasses import replace

from nasa_agent.context import SYSTEM_PROMPT, build_context, estimate_tokens, is_context_message
from tests.conftest import user
from tests.fakes import FakeInventoryBlock, FakeLLM, FakeMemory, assistant_text

BLOCK = "INVENTORY: E-1 unknown white pouch | NOD2O4 | open review"
SUMMARY = "GLOBAL: 5 live entities; Node 2 busy this morning."


def total_tokens(msgs) -> int:
    return sum(estimate_tokens(m.get("content") or "") for m in msgs)


def system_text(msgs) -> str:
    """System prompt + the delimited context-data message (review round 1 moved block/summary out of the system
    role; see test_ac_agt_07_untrusted_context.py)."""
    return "\n".join(m["content"] for m in msgs if m["role"] == "system" or is_context_message(m))


def chat_part(msgs):
    return [m for m in msgs if m["role"] != "system" and not is_context_message(m)]


def long_history(n_turns: int, words: int = 150):
    hist = []
    for i in range(n_turns):
        hist.append({"role": "user", "content": f"question {i} " + "lorem " * words})
        hist.append({"role": "assistant", "content": f"answer {i} " + "ipsum " * words})
    hist.append({"role": "user", "content": "LATEST: where is the drill?"})
    return hist


# ---------------------------------------------------------------- system prompt (privacy)
def test_ac_agt_07_system_prompt_has_crew_privacy_rules():
    text = SYSTEM_PROMPT.lower()
    assert "role" in text and "appearance" in text
    assert "name" in text, "must tell the model not to identify crew by name"
    assert "crew" in text


def test_ac_agt_07_system_prompt_demands_grounding_in_tools():
    text = SYSTEM_PROMPT.lower()
    assert "tool" in text
    assert "cite" in text or "citation" in text


# ---------------------------------------------------------------- estimate_tokens
def test_ac_agt_07_estimate_tokens_basic_properties():
    assert estimate_tokens("") == 0
    assert estimate_tokens(None) == 0
    assert estimate_tokens("hello") >= 1
    short, longer = estimate_tokens("drill " * 10), estimate_tokens("drill " * 100)
    assert longer > short
    assert 50 <= estimate_tokens("a" * 400) <= 200
    assert estimate_tokens("same text") == estimate_tokens("same text")


# ---------------------------------------------------------------- build_context
def test_ac_agt_07_layout_system_then_history():
    hist = [{"role": "user", "content": "hi"}, {"role": "assistant", "content": "hello"},
            {"role": "user", "content": "where is the drill?"}]
    msgs = build_context(hist, system_prompt=SYSTEM_PROMPT or "SYS", inventory_block=BLOCK, global_summary=SUMMARY,
                         token_budget=10_000)
    assert msgs[0]["role"] == "system"
    assert chat_part(msgs) == hist
    sys_text = system_text(msgs)
    assert BLOCK in sys_text and SUMMARY in sys_text


def test_ac_agt_07_order_system_prompt_block_summary():
    msgs = build_context(user("q"), system_prompt="SYSTEM-PROMPT-XYZ", inventory_block=BLOCK,
                         global_summary=SUMMARY, token_budget=10_000)
    s = system_text(msgs)
    assert s.index("SYSTEM-PROMPT-XYZ") < s.index(BLOCK) < s.index(SUMMARY)


def test_ac_agt_07_no_global_summary_is_fine():
    msgs = build_context(user("q"), system_prompt="SYS", inventory_block=BLOCK, global_summary=None,
                         token_budget=10_000)
    assert BLOCK in system_text(msgs)
    assert chat_part(msgs) == user("q")


def test_ac_agt_07_budget_respected_and_oldest_turns_dropped_first():
    hist = long_history(20)
    budget = 1200
    msgs = build_context(hist, system_prompt="SYS", inventory_block=BLOCK, global_summary=SUMMARY,
                         token_budget=budget)
    assert total_tokens(msgs) <= budget
    kept = chat_part(msgs)
    assert kept, "the latest user message must survive"
    assert kept[-1]["content"] == "LATEST: where is the drill?"
    assert kept == hist[len(hist) - len(kept):], "kept history must be a contiguous suffix (oldest dropped first)"
    assert len(kept) < len(hist)


def test_ac_agt_07_dropping_never_starts_history_with_dangling_assistant_reply():
    hist = long_history(20)
    msgs = build_context(hist, system_prompt="SYS", inventory_block=BLOCK, global_summary=None, token_budget=900)
    assert chat_part(msgs)[0]["role"] == "user"


def test_ac_agt_07_inventory_block_and_latest_message_never_dropped_under_pressure():
    hist = long_history(30)
    block = "INVENTORY: " + " | ".join(f"E-{i} item {i} LAB1S2" for i in range(40))
    budget = estimate_tokens(block) + estimate_tokens("SYS") + 200
    msgs = build_context(hist, system_prompt="SYS", inventory_block=block, global_summary=SUMMARY,
                         token_budget=budget)
    assert block in system_text(msgs)
    assert chat_part(msgs)[-1]["content"] == "LATEST: where is the drill?"
    assert total_tokens(msgs) <= budget


def test_ac_agt_07_huge_global_summary_is_cut_before_inventory_or_question():
    huge = "GLOBAL " + "chatter " * 6000
    budget = 1500
    msgs = build_context(long_history(3), system_prompt="SYS", inventory_block=BLOCK, global_summary=huge,
                         token_budget=budget)
    assert total_tokens(msgs) <= budget
    assert BLOCK in system_text(msgs)
    assert chat_part(msgs)[-1]["content"] == "LATEST: where is the drill?"
    assert huge not in system_text(msgs)


def test_ac_agt_07_bigger_budget_keeps_more_history():
    hist = long_history(20)
    small = build_context(hist, system_prompt="SYS", inventory_block=BLOCK, global_summary=None, token_budget=800)
    big = build_context(hist, system_prompt="SYS", inventory_block=BLOCK, global_summary=None, token_budget=4000)
    assert len(chat_part(big)) > len(chat_part(small))


def test_ac_agt_07_does_not_mutate_input_history():
    hist = long_history(5)
    snapshot = [dict(m) for m in hist]
    build_context(hist, system_prompt="SYS", inventory_block=BLOCK, global_summary=SUMMARY, token_budget=500)
    assert hist == snapshot


# ---------------------------------------------------------------- harness uses the context builder
async def test_ac_agt_07_harness_sends_system_prompt_and_global_memory(make_harness):
    llm = FakeLLM([assistant_text("hi")])
    mem = FakeMemory(global_summary="GLOBAL-MEMORY-SENTINEL")
    await make_harness(llm, memory=mem).run(user("hello"))
    first = llm.requests[0]["messages"]
    assert first[0]["role"] == "system"
    s = system_text(first)
    assert SYSTEM_PROMPT and SYSTEM_PROMPT in s
    assert "GLOBAL-MEMORY-SENTINEL" in s


async def test_ac_agt_07_harness_applies_configured_budget(make_harness, plain_settings):
    llm = FakeLLM([assistant_text("hi")])
    settings = replace(plain_settings, context_token_budget=1500)
    hist = long_history(40)
    await make_harness(llm, settings=settings, inventory_block=FakeInventoryBlock(BLOCK)).run(hist)
    sent = llm.requests[0]["messages"]
    assert total_tokens(sent) <= 1500
    assert chat_part(sent)[-1]["content"] == "LATEST: where is the drill?"
    assert len(chat_part(sent)) < len(hist)


async def test_ac_agt_07_harness_works_without_global_memory(make_harness):
    llm = FakeLLM([assistant_text("hi")])
    await make_harness(llm, memory=FakeMemory()).run(user("hello"))
    assert llm.requests[0]["messages"][0]["role"] == "system"
