"""AC-AGT-10 inventory-first memory: every chat turn renders a budgeted, SQL-derived inventory block (open reviews,
recently moved items, items in the asked-about module) that is never summarised away. Dreaming is auxiliary only.

``InventoryBlockRenderer(database_url, recent_hours=24).render(question, token_budget, now)`` -> str. Each entity line
carries its entity_id, label and location. Module resolution accepts location ids ("NOD2", "LAB1") and display names
("Node 2", "Destiny").
"""
from __future__ import annotations

from datetime import timedelta

import pytest

from nasa_agent.context import estimate_tokens, is_context_message
from nasa_agent.inventory_block import InventoryBlockRenderer
from tests.conftest import make_entity, make_event, make_review, table_counts, ts, user
from tests.fakes import FakeInventoryBlock, FakeLLM, FakeMemory, assistant_text, assistant_tools, tool_call

NOW = ts(60)


def context_text(messages) -> str:
    """System prompt + delimited context-data message (review round 1 moved the block out of the system role)."""
    return "\n".join(m.get("content") or "" for m in messages if m["role"] == "system" or is_context_message(m))


@pytest.fixture()
def world(db):
    w = {}
    w["pouch"] = make_entity(db, label="unknown white pouch", status="unknown", last_location="NOD2O4",
                             last_seen_at=ts(10), needs_review=True)
    w["review"] = make_review(db, entity_id=w["pouch"], description="white pouch with velcro", location_id="NOD2O4",
                              observed_at=ts(10))
    w["resolved_thing"] = make_entity(db, label="resolved gizmo", status="confirmed", last_location="COL1A1",
                                      last_seen_at=ts(-600))
    make_review(db, entity_id=w["resolved_thing"], status="resolved", location_id="COL1A1", observed_at=ts(-600))
    w["camera"] = make_entity(db, label="Nikon camera", catalog_id="camera-dslr", last_location="NOD2D4",
                              last_seen_at=ts(40))
    make_event(db, w["camera"], occurred_at=ts(30), actor_desc="crew member, red shirt", from_location="LAB1P3",
               to_location="NOD2D4")
    w["old_mover"] = make_entity(db, label="duct tape roll", catalog_id="duct-tape", last_location="JPM1F5",
                                 last_seen_at=ts(-60 * 48))
    make_event(db, w["old_mover"], occurred_at=ts(-60 * 48), actor_desc="crew member, green shirt",
               from_location="LAB1P3", to_location="JPM1F5")
    w["drill"] = make_entity(db, label="pistol-grip drill", catalog_id="drill-pistol", last_location="LAB1S2",
                             last_seen_at=ts(20))
    w["laptop"] = make_entity(db, label="SSC laptop", catalog_id="laptop", last_location="NOD2",
                              last_seen_at=ts(5))
    w["merged"] = make_entity(db, label="laptop duplicate", status="merged", merged_into=w["laptop"],
                              last_location="NOD2D4", last_seen_at=ts(5))
    w["dismissed"] = make_entity(db, label="lens glare", status="dismissed", last_location="NOD2D4",
                                 last_seen_at=ts(5))
    return w


@pytest.fixture()
def renderer(database_url):
    return InventoryBlockRenderer(database_url, recent_hours=24)


def test_ac_agt_10_block_lists_open_reviews(renderer, world):
    block = renderer.render("hello", token_budget=2000, now=NOW)
    assert world["pouch"] in block and "unknown white pouch" in block and "NOD2O4" in block


def test_ac_agt_10_resolved_reviews_not_listed(renderer, world):
    block = renderer.render("hello", token_budget=2000, now=NOW)
    assert world["resolved_thing"] not in block


def test_ac_agt_10_block_lists_recently_moved_items_with_actor(renderer, world):
    block = renderer.render("hello", token_budget=2000, now=NOW)
    assert world["camera"] in block and "NOD2D4" in block
    assert "crew member, red shirt" in block


def test_ac_agt_10_moves_outside_recent_window_not_listed(renderer, world):
    block = renderer.render("hello", token_budget=2000, now=NOW)
    assert world["old_mover"] not in block


@pytest.mark.parametrize("question", ["what is unknown in Node 2?", "anything new in NOD2?", "list Node 2 Harmony"])
def test_ac_agt_10_items_in_asked_about_module(renderer, world, question):
    block = renderer.render(question, token_budget=2000, now=NOW)
    assert world["laptop"] in block            # located at module level (NOD2)
    assert world["camera"] in block            # NOD2D4 is a zone under NOD2
    assert world["drill"] not in block         # LAB1S2 is not in Node 2


@pytest.mark.parametrize("question", ["what is in the Destiny lab?", "items in LAB1"])
def test_ac_agt_10_lab_module_by_id_or_display_name(renderer, world, question):
    block = renderer.render(question, token_budget=2000, now=NOW)
    assert world["drill"] in block
    assert world["laptop"] not in block


def test_ac_agt_10_module_items_only_when_asked(renderer, world):
    block = renderer.render("hello", token_budget=2000, now=NOW)
    assert world["drill"] not in block and world["laptop"] not in block


def test_ac_agt_10_merged_and_dismissed_never_listed(renderer, world):
    block = renderer.render("what is in Node 2?", token_budget=2000, now=NOW)
    assert world["merged"] not in block and world["dismissed"] not in block


def test_ac_agt_10_block_respects_token_budget_and_keeps_open_reviews_first(renderer, world, db):
    for i in range(80):
        make_entity(db, label=f"food pack variant number {i} with a long descriptive label", last_location="NOD2D4",
                    last_seen_at=ts(1))
    budget = 150
    block = renderer.render("what is in Node 2?", token_budget=budget, now=NOW)
    assert estimate_tokens(block) <= budget
    assert world["pouch"] in block, "open reviews have priority over module listings"


def test_ac_agt_10_block_is_deterministic(renderer, world):
    a = renderer.render("what is in Node 2?", token_budget=2000, now=NOW)
    b = renderer.render("what is in Node 2?", token_budget=2000, now=NOW)
    assert a == b and a.strip()


def test_ac_agt_10_block_reads_only(renderer, world, db):
    before = table_counts(db)
    renderer.render("what is in Node 2?", token_budget=2000, now=NOW)
    assert table_counts(db) == before


def test_ac_agt_10_block_is_sql_truth_not_memory(renderer, world, db):
    """A contradicting dreaming summary does not change the block: it is rendered from current SQL state."""
    db.execute("INSERT INTO agent_memory (scope, scope_key, summary) VALUES ('entity', %s, 'camera is in LAB1P3')",
               (world["camera"],))
    block = renderer.render("where is the camera in Node 2?", token_budget=2000, now=NOW)
    assert "NOD2D4" in block
    assert "camera is in LAB1P3" not in block


def test_ac_agt_10_recent_hours_window_configurable(database_url, world):
    narrow = InventoryBlockRenderer(database_url, recent_hours=0.25)  # 15 min: camera moved 30 min before NOW
    assert world["camera"] not in narrow.render("hello", token_budget=2000, now=NOW)
    wide = InventoryBlockRenderer(database_url, recent_hours=72)
    assert world["old_mover"] in wide.render("hello", token_budget=2000, now=NOW + timedelta(minutes=1))


# ---------------------------------------------------------------- harness: every turn, every step
async def test_ac_agt_10_block_rendered_for_latest_question_each_turn(make_harness):
    block = FakeInventoryBlock("IWS-SENTINEL pouch NOD2O4")
    llm = FakeLLM([], default=assistant_text("a"))  # follow-up without tools is nudged once (AC-AGT-12)
    history = [{"role": "user", "content": "old q"}, {"role": "assistant", "content": "old a"},
               {"role": "user", "content": "what is unknown in Node 2?"}]
    await make_harness(llm, inventory_block=block).run(history)
    assert block.questions[-1] == "what is unknown in Node 2?"
    assert "IWS-SENTINEL" in context_text(llm.requests[0]["messages"])


async def test_ac_agt_10_block_present_in_every_step_of_the_loop(make_harness):
    block = FakeInventoryBlock("IWS-SENTINEL")
    llm = FakeLLM([assistant_tools(tool_call("where_is", {"name": "drill"})),
                   assistant_tools(tool_call("who_moved", {"name": "drill"})),
                   assistant_text("drill in LAB1S2")])
    await make_harness(llm, inventory_block=block).run(user("where is the drill and who moved it?"))
    assert llm.calls == 3
    for req in llm.requests:
        assert "IWS-SENTINEL" in context_text(req["messages"])


async def test_ac_agt_10_block_survives_long_history_and_large_memory(make_harness, plain_settings):
    from dataclasses import replace

    block = FakeInventoryBlock("IWS-SENTINEL " + "item line " * 50)
    mem = FakeMemory(global_summary="DREAM " + "chatter " * 5000)
    history = []
    for i in range(30):
        history += [{"role": "user", "content": "q " * 200}, {"role": "assistant", "content": "a " * 200}]
    history.append({"role": "user", "content": "LATEST"})
    llm = FakeLLM([], default=assistant_text("ok"))  # follow-up without tools is nudged once (AC-AGT-12)
    await make_harness(llm, inventory_block=block, memory=mem,
                       settings=replace(plain_settings, context_token_budget=1200)).run(history)
    sys_text = context_text(llm.requests[0]["messages"])
    assert block.text in sys_text, "inventory block must be included verbatim, never summarised or cut"


async def test_ac_agt_10_block_uses_configured_inventory_budget(make_harness, plain_settings):
    from dataclasses import replace

    seen = {}

    class Spy(FakeInventoryBlock):
        def render(self, question, token_budget, now=None):
            seen["budget"] = token_budget
            return super().render(question, token_budget, now)

    llm = FakeLLM([assistant_text("ok")])
    await make_harness(llm, inventory_block=Spy(),
                       settings=replace(plain_settings, inventory_block_token_budget=321)).run(user("q"))
    assert seen["budget"] == 321
