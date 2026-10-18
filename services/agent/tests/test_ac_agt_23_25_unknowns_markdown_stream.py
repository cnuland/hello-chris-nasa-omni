"""AC-AGT-23 (UI2-08: "What unknown objects are in Node 1?" answered "no record"), AC-AGT-24 (UI2-12: numbered lists
restart at "1.", raw ISO timestamps) and AC-AGT-25 (UI2-16: the whole answer arrived at once)."""
from __future__ import annotations

import asyncio
import time
from dataclasses import replace

from nasa_agent.config import Settings
from nasa_agent.harness import NO_RECORD_REPLY, normalize_markdown
from nasa_agent.main import stream_chat_events
from tests.conftest import user
from tests.fakes import FakeLLM, assistant_text, assistant_tools, tool_call
from tests.live_round2 import load_live


# ---------------------------------------------------------------- AC-AGT-23
async def test_ac_agt_23_unknown_location_is_a_validation_error(toolbox, fake_inventory):
    r = await toolbox.call("list_unknowns", {"location": "Atlantis"})
    assert not r.ok and r.error_kind == "validation"
    assert "NOD1" in r.error
    assert fake_inventory.calls("/items") == []


async def test_ac_agt_23_locations_unavailable_passes_value_through(toolbox, fake_inventory):
    async def boom():
        from nasa_agent.inventory_client import InventoryError
        raise InventoryError("down")

    toolbox.inventory.locations = boom
    r = await toolbox.call("list_unknowns", {"location": "NOD2"})
    assert r.ok and fake_inventory.params(fake_inventory.calls("/items")[-1])["location"] == ["NOD2"]


async def test_ac_agt_23_empty_unknowns_in_node1_answered_honestly(make_harness, fake_inventory, toolbox):
    load_live(fake_inventory, toolbox)
    for loc in ("Node 1", "NOD1"):
        llm = FakeLLM([assistant_tools(tool_call("list_unknowns", {"location": loc})),
                       assistant_text("I don't have a record of that.")])
        r = await make_harness(llm).run(user("What unknown objects are in Node 1?"))
        assert r["reply"] == "There are no unknown objects in Node 1 Unity (NOD1).", loc
        assert r["reply"] != NO_RECORD_REPLY


async def test_ac_agt_23_empty_unknowns_without_location(make_harness, fake_inventory):
    fake_inventory.items = [i for i in fake_inventory.items if i["status"] != "unknown"]
    llm = FakeLLM([assistant_tools(tool_call("list_unknowns", {})), assistant_text("None.")])
    r = await make_harness(llm).run(user("Are there any unknown objects?"))
    assert r["reply"] == "There are no unknown objects on record."


async def test_ac_agt_23_non_empty_unknowns_still_use_model_answer(make_harness):
    text = "There is one unknown object in Node 2: unknown white pouch in NOD2O4, seen at 10:00 UTC."
    llm = FakeLLM([assistant_tools(tool_call("list_unknowns", {"location": "Node 2"})), assistant_text(text)])
    r = await make_harness(llm).run(user("What unknown objects are in Node 2?"))
    assert r["reply"] == text


# ---------------------------------------------------------------- AC-AGT-24
def test_ac_agt_24_renumbers_and_flattens_lists():
    text = "Laptops:\n\n1. **Laptop** in USOS\n   - seen 22:51 UTC\n\n1. **Laptop** in NOD1\n\n1. **Laptop** in NOD3"
    assert normalize_markdown(text) == (
        "Laptops:\n\n1. **Laptop** in USOS; seen 22:51 UTC\n2. **Laptop** in NOD1\n3. **Laptop** in NOD3")


def test_ac_agt_24_separate_lists_restart():
    text = "Moved:\n1. a\n2. b\n\nAdded:\n1. c"
    assert normalize_markdown(text) == "Moved:\n1. a\n2. b\n\nAdded:\n1. c"


def test_ac_agt_24_iso_timestamps_use_dashboard_format():
    assert normalize_markdown("Seen at 2026-10-17T22:51:03Z.") == "Seen at 2026-10-17 22:51 UTC."
    assert normalize_markdown("at 2026-10-17T22:51:03.178660Z UTC") == "at 2026-10-17 22:51 UTC"
    assert normalize_markdown("at 2026-10-17T22:51+00:00") == "at 2026-10-17 22:51 UTC"


def test_ac_agt_24_plain_text_untouched():
    text = "The drill was used by a person in blue shirt at 22:53 UTC. Version 1.5 is fine."
    assert normalize_markdown(text) == text


async def test_ac_agt_24_harness_applies_normalisation(make_harness, fake_inventory, toolbox):
    load_live(fake_inventory, toolbox)
    text = ("1. laptop: USOS, 2026-10-17T22:51:03Z\n\n1. laptop: NOD1, 2026-10-17T23:00:27Z")
    llm = FakeLLM([assistant_tools(tool_call("where_is", {"name": "laptop"})), assistant_text(text)])
    r = await make_harness(llm).run(user("Where is the laptop right now?"))
    assert r["reply"] == "1. laptop: USOS, 2026-10-17 22:51 UTC\n2. laptop: NOD1, 2026-10-17 23:00 UTC"


# ---------------------------------------------------------------- AC-AGT-25
def test_ac_agt_25_token_delay_setting():
    base = {"AGENT_LLM_URL": "http://l/v1", "INVENTORY_API_URL": "http://i", "DATABASE_URL": "postgresql://x"}
    assert Settings.from_env(base).stream_token_delay_s == 0.03
    assert Settings.from_env({**base, "AGENT_STREAM_TOKEN_DELAY_S": "0"}).stream_token_delay_s == 0
    assert Settings.from_env({**base, "AGENT_STREAM_TOKEN_DELAY_S": "0.1"}).stream_token_delay_s == 0.1


async def _collect(reply: str, delay: float) -> tuple[list[str], float]:
    async def run(on_event):
        return {"reply": reply, "citations": []}

    started = time.perf_counter()
    frames = [f async for f in stream_chat_events(run, keepalive_s=5, request_id="r", token_delay_s=delay)]
    return frames, time.perf_counter() - started


async def test_ac_agt_25_tokens_are_paced_and_concatenate():
    import json

    reply = " ".join(f"word{i}" for i in range(30)) + "."
    frames, elapsed = await _collect(reply, 0.01)
    tokens = [json.loads(f.split("data: ", 1)[1]) ["text"] for f in frames if f.startswith("event: token")]
    assert "".join(tokens) == reply
    assert 5 <= len(tokens) <= 15, "a few words per chunk"
    assert elapsed >= 0.01 * (len(tokens) - 1) * 0.8


async def test_ac_agt_25_zero_delay_is_immediate():
    frames, elapsed = await _collect("a b c d e f g h", 0)
    assert elapsed < 0.5 and frames[-1].startswith("event: done")


async def test_ac_agt_25_disconnect_while_pacing_stops_cleanly():
    async def run(on_event):
        return {"reply": " ".join(["w"] * 200), "citations": []}

    gen = stream_chat_events(run, keepalive_s=5, request_id="r", token_delay_s=0.05)
    assert (await asyncio.wait_for(gen.__anext__(), 2)).startswith(": stream open")  # AC-AGT-31
    first = await asyncio.wait_for(gen.__anext__(), 2)
    assert first.startswith("event: token")
    await gen.aclose()


def test_ac_agt_25_app_streams_with_configured_delay(plain_settings, inventory, fake_memory):
    from fastapi.testclient import TestClient

    from nasa_agent.main import create_app
    from tests.fakes import FakeInventoryBlock
    from tests.test_ac_agt_01_chat_api import parse_sse

    llm = FakeLLM([assistant_tools(tool_call("where_is", {"name": "drill"})),
                   assistant_text("The pistol-grip drill was last seen in LAB1S2 at 12:30 UTC, by the rack.")])
    app = create_app(replace(plain_settings, stream_token_delay_s=0.001), llm=llm, inventory=inventory,
                     memory=fake_memory, inventory_block=FakeInventoryBlock(), dreamer=None)
    with TestClient(app) as c:
        events = parse_sse(c.post("/chat/stream", json={"messages": user("where is the drill?")}).text)
    done = events[-1][1]
    assert "".join(d["text"] for n, d in events if n == "token") == done["reply"]
