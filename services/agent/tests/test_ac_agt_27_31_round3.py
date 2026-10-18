"""UI review round 3 (docs/ui-review/round3): AC-AGT-27 people_seen (UI3-01, P1: "4 people" vs 17 tracks), AC-AGT-28
counting questions use an uncapped path, AC-AGT-29 deterministic operator-English lists + paging (UI3-10), AC-AGT-30
person events (UI3-04) and AC-AGT-31 visible streaming (PARTIAL UI2-16)."""
from __future__ import annotations

import asyncio
import itertools
import json
import logging
import socket
import threading
import time
from dataclasses import replace

import pytest

from nasa_agent.config import Settings
from nasa_agent.context import SYSTEM_PROMPT
from nasa_agent.harness import NO_RECORD_REPLY
from nasa_agent.main import stream_chat_events
from nasa_agent.render import count_claims, is_show_more
from nasa_agent.tools import GROUNDING_TOOLS, READ_TOOLS
from tests.conftest import user
from tests.fakes import FakeInventoryBlock, FakeLLM, _item, assistant_text, assistant_tools, tool_call
from tests.live_round2 import load_live
from tests.live_round3 import (
    CTB,
    EXPECTED_BY_MODULE,
    LIVE_PEOPLE,
    LIVE_PEOPLE_DRAFT,
    PERSON_87,
    PERSON_CUP,
    load_people,
)

PEOPLE_Q = "How many people have been seen today, and where?"
#: AC-AGT-35 renders a multi-item where_is result for a where-question; this question keeps the model's wording.
MODEL_WRITTEN_Q = "Which laptops do we have?"
PERSON_IDS = {p[0] for p in LIVE_PEOPLE}


@pytest.fixture()
def people(toolbox, fake_inventory):
    load_people(fake_inventory, toolbox)
    return toolbox


def quiet_new_tracks(fake_inventory) -> None:
    """Person tracks first seen before the last hour, so "what changed" lists only the three live events."""
    for i in fake_inventory.items:
        if i["kind"] == "person":
            i["first_seen_at"] = "2026-10-18T00:30:00Z"


def by_module(data) -> dict[str, int]:
    return {m["location"]: m["count"] for m in data["by_module"]}


# ================================================================ AC-AGT-27 people_seen
def test_ac_agt_27_registered_as_read_and_grounding_tool(toolbox):
    assert "people_seen" in READ_TOOLS and "people_seen" in GROUNDING_TOOLS
    spec = {t["function"]["name"]: t["function"] for t in toolbox.specs()}["people_seen"]
    assert set(spec["parameters"]["properties"]) == {"since", "until", "location", "offset"}
    assert toolbox.is_read_only("people_seen")


async def test_ac_agt_27_live_counts_match_the_api(people, fake_inventory):
    r = await people.call("people_seen", {})
    assert r.ok and r.has_data
    d = r.data
    assert d["total_tracks"] == 17
    assert by_module(d) == EXPECTED_BY_MODULE
    assert d["since"] == "2026-10-18T00:00:00Z", "default window: start of the UTC day"
    assert d["complete"] is True
    names = {m["location"]: m["location_name"] for m in d["by_module"]}
    assert names["CUP1"] == "Cupola" and names["USOS"] == "US Orbital Segment"
    assert [m["count"] for m in d["by_module"]] == sorted((m["count"] for m in d["by_module"]), reverse=True)
    q = fake_inventory.params(fake_inventory.calls("/items")[0])
    assert q["kind"] == ["person"] and q["seen_after"] == ["2026-10-18T00:00:00Z"]


async def test_ac_agt_27_pages_with_offset_until_total(people, fake_inventory):
    fake_inventory.max_page = 5
    r = await people.call("people_seen", {})
    assert r.data["total_tracks"] == 17 and by_module(r.data) == EXPECTED_BY_MODULE
    offsets = [fake_inventory.params(c).get("offset", ["0"])[0] for c in fake_inventory.calls("/items")]
    assert offsets == ["0", "5", "10", "15"]


async def test_ac_agt_27_row_cap_reports_incomplete(people, fake_inventory, monkeypatch):
    from nasa_agent import tools

    monkeypatch.setattr(tools, "MAX_SCAN_ROWS", 10)
    fake_inventory.max_page = 5
    r = await people.call("people_seen", {})
    assert r.data["complete"] is False and r.data["total_tracks"] == 17, "the API total, not the scanned rows"


async def test_ac_agt_27_tracks_carry_appearance_and_last_seen(people):
    r = await people.call("people_seen", {})
    tracks = {t["entity_id"]: t for t in r.data["tracks"]}
    assert len(tracks) == 17 and r.data["tracks_omitted"] == 0
    t = tracks[PERSON_CUP]
    assert t["appearance"] == "blue shorts, grey long-sleeve shirt"
    assert t["location"] == "CUP1" and t["location_name"] == "Cupola"
    assert t["last_seen_at"].startswith("2026-10-18T03:21") and t["first_seen_at"].startswith("2026-10-18T03:06")
    assert tracks["90bbed98-b768-4949-a063-4b439486aa8a"]["appearance"] == "blue shirt and shorts"
    assert r.data["tracks"][0]["entity_id"] == "cef63c49-f74f-4c1e-881e-16d2c5880c02", "newest first"


async def test_ac_agt_27_caveat_is_honest_and_never_states_crew_size(people):
    r = await people.call("people_seen", {})
    caveat = r.data["caveat"].lower()
    assert "approximate" in caveat and "non-biometric" in caveat and "over-count" in caveat
    assert "clothes" in caveat and "module" in caveat
    assert not any(ch.isdigit() for ch in r.data["caveat"]), "the crew size is unknown to the system"


async def test_ac_agt_27_citations_are_person_tracks_only(people):
    r = await people.call("people_seen", {})
    assert {c["entity_id"] for c in r.citations} == PERSON_IDS
    assert all(c["at"] for c in r.citations)


async def test_ac_agt_27_location_filter_and_aliases(people, fake_inventory):
    r = await people.call("people_seen", {"location": "Node 1"})
    assert r.data["location"] == "NOD1" and r.data["total_tracks"] == 2
    assert fake_inventory.params(fake_inventory.calls("/items")[-1])["location"] == ["NOD1"]
    n = len(fake_inventory.calls("/items"))
    bad = await people.call("people_seen", {"location": "Atlantis"})
    assert not bad.ok and bad.error_kind == "validation" and len(fake_inventory.calls("/items")) == n


async def test_ac_agt_27_window_validation(people):
    future = await people.call("people_seen", {"since": "2026-10-19T00:00:00Z"})
    assert not future.ok and future.error_kind == "validation"
    old = await people.call("people_seen", {"since": "2026-10-01T00:00:00Z"})
    assert not old.ok and old.error_kind == "validation"
    naive = await people.call("people_seen", {"since": "2026-10-18T00:00:00"})
    assert not naive.ok and naive.error_kind == "validation"


async def test_ac_agt_27_until_drops_tracks_first_seen_later(people):
    r = await people.call("people_seen", {"since": "2026-10-18T00:00:00Z", "until": "2026-10-18T03:06:00Z"})
    assert r.data["total_tracks"] == 5, "tracks whose first sighting is before 03:06"


async def test_ac_agt_27_hidden_and_non_person_rows_are_dropped(people, fake_inventory):
    merged = _item("12121212-1212-4121-8121-121212121212", "person", kind="person", status="merged",
                   last_location="USOS", last_seen_at="2026-10-18T03:00:00Z")
    fake_inventory.items.append(merged)

    orig = fake_inventory._list_items

    def leaky(q):  # an API that ignores status/kind defaults must not inflate the count
        page = orig({k: v for k, v in q.items() if k != "kind"})
        page["items"] += [merged]
        return page

    fake_inventory._list_items = leaky
    r = await people.call("people_seen", {})
    assert r.data["total_tracks"] == 17


async def test_ac_agt_27_offset_pages_tracks(people):
    r = await people.call("people_seen", {"offset": 10})
    assert len(r.data["tracks"]) == 7 and r.data["offset"] == 10 and r.data["total_tracks"] == 17
    assert len(r.citations) == 7


async def test_ac_agt_27_empty_window_still_grounds(people, fake_inventory):
    fake_inventory.items = [i for i in fake_inventory.items if i["kind"] != "person"]
    r = await people.call("people_seen", {})
    assert r.ok and r.has_data and r.data["total_tracks"] == 0 and r.data["by_module"] == []


# ================================================================ AC-AGT-28 counting questions
def test_ac_agt_28_count_items_registered(toolbox):
    assert "count_items" in READ_TOOLS and "count_items" in GROUNDING_TOOLS


async def test_ac_agt_28_count_items_uses_api_total_and_by_module(toolbox, fake_inventory):
    load_live(fake_inventory, toolbox)
    fake_inventory.max_page = 2
    r = await toolbox.call("count_items", {"q": "laptop"})
    assert r.ok and r.data["total"] == 5 and r.data["complete"] is True
    assert by_module(r.data) == {"USOS": 2, "NOD1": 2, "NOD3": 1}


def test_ac_agt_28_routing_prompt_names_the_counting_tools():
    text = " ".join(SYSTEM_PROMPT.split())
    assert "people_seen" in text and "count_items" in text
    assert "never count the rows of a list" in text.lower()
    assert "7 crew" not in text


async def test_ac_agt_28_live_people_question_uses_the_track_total(make_harness, people):
    """The reviewer's case: the model used recent_changes and wrote "4 people"; the answer must be the 17 tracks."""
    llm = FakeLLM([assistant_tools(tool_call("recent_changes", {})), assistant_text(LIVE_PEOPLE_DRAFT)],
                  default=assistant_text(LIVE_PEOPLE_DRAFT))
    r = await make_harness(llm).run(user(PEOPLE_Q))
    reply = r["reply"]
    assert "17 person tracks" in reply and "4 people" not in reply
    for frag in ("US Orbital Segment (USOS) 10", "Node 1 Unity (NOD1) 2", "Node 3 Tranquility (NOD3) 2",
                 "Cupola (CUP1) 1", "Zvezda Service Module (ZVEZDA) 1", "Columbus (COL1) 1"):
        assert frag in reply, frag
    assert "approximate" in reply and "non-biometric" in reply
    assert "between 2026-10-18 00:00 and 2026-10-18 03:32 UTC" in reply, "today = since the start of the UTC day"
    assert any(t["name"] == "people_seen" and t["ok"] for t in r["tool_calls"])
    assert r["citations"] and {c["entity_id"] for c in r["citations"]} <= PERSON_IDS, "cite person items only"


async def test_ac_agt_28_model_calling_people_seen_gets_the_rendered_answer(make_harness, people):
    llm = FakeLLM([assistant_tools(tool_call("people_seen", {"since": "2026-10-18T00:00:00Z"}))])
    r = await make_harness(llm).run(user(PEOPLE_Q))
    assert llm.calls == 0, "AC-AGT-35: a bare people-count question is routed without any LLM call"
    assert r["reply"].startswith("17 person tracks were seen between 2026-10-18 00:00 and 2026-10-18 03:32 UTC")


async def test_ac_agt_28_people_count_without_any_tool_is_still_answered(make_harness, people):
    llm = FakeLLM([assistant_text("There are 7 crew members aboard.")])
    r = await make_harness(llm).run(user("How many crew members were seen in the last hour?"))
    assert "7 crew" not in r["reply"] and "person tracks" in r["reply"]
    ps = next(t for t in r["tool_calls"] if t["name"] == "people_seen")
    assert ps["arguments"]["since"] == "2026-10-18T02:32:00Z"


async def test_ac_agt_28_people_count_in_a_named_module(make_harness, people):
    llm = FakeLLM([assistant_text("Two.")])
    r = await make_harness(llm).run(user("How many people are in Node 1 today?"))
    assert r["reply"].startswith("2 person tracks were seen in Node 1 Unity (NOD1) between")


async def test_ac_agt_28_handling_question_is_not_forced(make_harness, toolbox, fake_inventory):
    load_live(fake_inventory, toolbox)
    llm = FakeLLM([assistant_tools(tool_call("who_moved", {"name": "drill"})),
                   assistant_text("One person used the drill at 22:53 UTC.")])
    r = await make_harness(llm).run(user("How many people used the drill?"))
    assert not any(t["name"] == "people_seen" for t in r["tool_calls"])


async def test_ac_agt_28_item_count_runs_count_items_with_the_models_filters(make_harness, toolbox, fake_inventory):
    load_live(fake_inventory, toolbox)
    for k in range(3):
        fake_inventory.items.append(_item(f"abcdef0{k}-0000-4000-8000-000000000000", "laptop", status="provisional",
                                          last_location="LAB1", last_seen_at="2026-10-17T20:00:00Z"))
    llm = FakeLLM([assistant_tools(tool_call("where_is", {"name": "laptop"})),
                   assistant_text("There are 5 laptops.")])
    r = await make_harness(llm).run(user("How many laptops are there?"))
    assert r["reply"].startswith("8 inventory items match \"laptop\"")
    assert "5 laptops" not in r["reply"]
    assert any(t["name"] == "count_items" and t["arguments"] == {"q": "laptop"} for t in r["tool_calls"])


async def test_ac_agt_28_count_claim_backed_by_api_total_is_kept(make_harness, toolbox, fake_inventory):
    load_live(fake_inventory, toolbox)
    text = "There are 5 laptops: two in USOS, two in NOD1 and one in NOD3."
    llm = FakeLLM([assistant_tools(tool_call("where_is", {"name": "laptop"})), assistant_text(text)])
    r = await make_harness(llm).run(user(MODEL_WRITTEN_Q))
    assert r["reply"] == text


async def test_ac_agt_28_count_claim_from_a_capped_list_is_rejected(make_harness, toolbox, fake_inventory, caplog):
    caplog.set_level(logging.INFO, logger="nasa_agent.harness")
    load_live(fake_inventory, toolbox)
    for k in range(3):
        fake_inventory.items.append(_item(f"abcdef0{k}-0000-4000-8000-000000000000", "laptop", status="provisional",
                                          last_location="LAB1", last_seen_at="2026-10-17T20:00:00Z"))
    text = "There are 5 laptops in the inventory."
    llm = FakeLLM([assistant_tools(tool_call("where_is", {"name": "laptop"})), assistant_text(text)])
    r = await make_harness(llm).run(user(MODEL_WRITTEN_Q))
    assert "5 laptops" not in r["reply"] and r["reply"] != NO_RECORD_REPLY
    rec = next(x for x in caplog.records if x.getMessage() == "chat turn")
    assert rec.fallback == "count_unverified"


def test_ac_agt_28_count_claims_ignore_times_dates_ids_and_durations():
    text = ("In the last 2 hours, the laptop in Node 1 Unity (NOD1) was seen at 03:14 UTC on 2026-10-18; "
            "1. laptop: NOD3\n2. laptop: JPM1")
    assert count_claims(text, counting=False) == []
    assert count_claims(text, counting=True) == []
    assert count_claims("There are 4 people in USOS.", counting=False) == [4]
    assert count_claims("Seven crew members are aboard.", counting=False) == [7]
    assert count_claims("USOS: 10, NOD1: 2", counting=True) == [10, 2]
    assert count_claims("USOS: 10, NOD1: 2", counting=False) == []


async def test_ac_agt_28_model_reply_with_people_data_gets_the_caveat(make_harness, people):
    llm = FakeLLM([assistant_tools(tool_call("people_seen", {}), tool_call("where_is", {"name": "laptop"})),
                   assistant_text("17 person tracks were seen today; the most recent was in USOS at 03:31 UTC.")])
    r = await make_harness(llm).run(user("Who has been around today?"))
    assert r["reply"].startswith("17 person tracks were seen today")
    assert "non-biometric" in r["reply"]


# ================================================================ AC-AGT-29 operator English + paging
async def test_ac_agt_29_recent_changes_render_deterministically(make_harness, people, fake_inventory):
    quiet_new_tracks(fake_inventory)
    replies = []
    for _ in range(2):
        llm = FakeLLM([assistant_tools(tool_call("recent_changes", {}))])
        r = await make_harness(llm).run(user("What changed in the last hour?"))
        assert llm.calls == 0, "rendered with no LLM call (AC-AGT-35 routing)"
        replies.append(r["reply"])
    assert replies[0] == replies[1], "the same format in every run"
    reply = replies[0]
    lines = reply.split("\n")
    assert lines[0] == "Changes between 2026-10-18 02:32 and 2026-10-18 03:32 UTC (showing 1-3 of 3):"
    assert ("1. 2026-10-18 03:22 UTC: Cargo transfer bag moved from US Orbital Segment (USOS) to Kibo JEM "
            "Pressurized Module (JPM1) by operator ui-review-3") in lines
    for bad in ("new_item", "actor:", "moved: person", "by person (spacesuit", "more;"):
        assert bad not in reply


async def test_ac_agt_29_long_lists_end_with_a_paging_hint_and_show_more_pages(make_harness, people, fake_inventory):
    quiet_new_tracks(fake_inventory)
    for k in range(25):
        fake_inventory.events.append({
            "event_id": f"ev-{k:02d}", "event_type": "used", "entity_id": CTB, "actor_desc": "grey hoodie",
            "from_location": "JPM1", "to_location": None, "occurred_at": f"2026-10-18T02:{35 + k:02d}:00Z",
            "confidence": 0.8, "source": "pipeline", "entity_label": "cargo transfer bag"})
    llm = FakeLLM([assistant_tools(tool_call("recent_changes", {}))])
    first = await make_harness(llm).run(user("What changed in the last hour?"))
    assert "(showing 1-10 of 28)" in first["reply"]
    assert first["reply"].endswith("and 18 more; ask 'show more changes' to continue.")
    history = [*user("What changed in the last hour?"), {"role": "assistant", "content": first["reply"]},
               *user("show more")]
    nollm = FakeLLM([])
    second = await make_harness(nollm).run(history)
    assert nollm.calls == 0, "paging is answered in code"
    assert "(showing 11-20 of 28)" in second["reply"]
    assert second["reply"].split("\n")[1].startswith("11. ")
    call = second["tool_calls"][0]
    assert call["name"] == "recent_changes" and call["arguments"]["offset"] == 10
    assert call["arguments"]["since"] == "2026-10-18T02:32:00Z"
    assert [c["entity_id"] for c in second["citations"]] == [CTB], "AC-AGT-33: one citation per entity"
    third = await make_harness(FakeLLM([])).run(
        [*history, {"role": "assistant", "content": second["reply"]}, *user("Show more changes please")])
    assert "(showing 21-28 of 28)" in third["reply"] and "more;" not in third["reply"]


async def test_ac_agt_29_show_more_people_pages_tracks(make_harness, people):
    first = await make_harness(FakeLLM([assistant_tools(tool_call("people_seen", {}))])).run(user(PEOPLE_Q))
    assert first["reply"].endswith("and 7 more; ask 'show more people' to continue.")
    history = [*user(PEOPLE_Q), {"role": "assistant", "content": first["reply"]}, *user("more")]
    second = await make_harness(FakeLLM([])).run(history)
    assert "(showing 11-17 of 17)" in second["reply"]
    assert second["tool_calls"][0]["arguments"]["offset"] == 10


def test_ac_agt_29_show_more_detection():
    for q in ("show more", "More", "next", "continue", "show more changes", "Show me the rest.", "more please"):
        assert is_show_more(q), q
    for q in ("show more details about the drill", "where is the drill?", "more drills?", ""):
        assert not is_show_more(q), q


async def test_ac_agt_29_show_more_without_a_paged_reply_uses_the_llm(make_harness):
    history = [*user("Where is the drill?"), {"role": "assistant", "content": "It is in LAB1S2."}, *user("more")]
    llm = FakeLLM([assistant_tools(tool_call("where_is", {"name": "drill"})),
                   assistant_text("The pistol-grip drill was last seen in LAB1S2 at 12:30 UTC.")])
    r = await make_harness(llm).run(history)
    assert llm.calls >= 1 and "LAB1S2" in r["reply"]


async def test_ac_agt_29_recent_changes_offset_and_total(people, fake_inventory):
    quiet_new_tracks(fake_inventory)
    r = await people.call("recent_changes", {"since": "2026-10-18T02:32:00Z", "offset": 1})
    assert r.data["total"] == 3 and r.data["offset"] == 1 and len(r.data["changes"]) == 2


async def test_ac_agt_29_grounded_summary_uses_operator_english(make_harness, toolbox, fake_inventory):
    load_live(fake_inventory, toolbox)
    llm = FakeLLM([assistant_tools(tool_call("where_is", {"name": "laptop"})),
                   assistant_text("The laptop was moved by the commander at 21:10.")])
    r = await make_harness(llm).run(user("Where is the laptop?"))
    assert "Laptop: last seen in Node 1 Unity (NOD1) at 2026-10-17 23:00 UTC" in r["reply"]


# ================================================================ AC-AGT-30 person events
async def test_ac_agt_30_new_person_track_reads_first_seen(make_harness, people):
    llm = FakeLLM([assistant_tools(tool_call("recent_changes", {}))])
    reply = (await make_harness(llm).run(user("What changed in the last hour?")))["reply"]
    assert "2026-10-18 03:22 UTC: Person (dark hair, purple jacket) first seen in US Orbital Segment (USOS)" in reply
    assert "New person" not in reply


async def test_ac_agt_30_recent_changes_marks_person_subjects_and_drops_their_actor(people):
    r = await people.call("recent_changes", {"since": "2026-10-18T02:32:00Z"})
    c = next(x for x in r.data["changes"] if x["entity_id"] == PERSON_87)
    assert c["subject_kind"] == "person" and c["appearance"] == "Short hair, blue shorts, orange top"
    assert c.get("actor") is None
    obj = next(x for x in r.data["changes"] if x["entity_id"] == CTB)
    assert obj.get("subject_kind") != "person" and obj["actor"] == "ui-review-3"


async def test_ac_agt_30_person_lines_read_as_seen_leaving_or_arriving(make_harness, people, fake_inventory):
    quiet_new_tracks(fake_inventory)
    llm = FakeLLM([assistant_tools(tool_call("recent_changes", {}))])
    reply = (await make_harness(llm).run(user("What changed in the last hour?")))["reply"]
    assert "2026-10-18 03:10 UTC: Person (Short hair, blue shorts, orange top) left US Orbital Segment (USOS)" in reply
    assert "2026-10-18 03:21 UTC: Person (blue shorts, grey long-sleeve shirt) seen in Cupola (CUP1)" in reply
    assert "spacesuit" not in reply and "grey t-shirt" not in reply
    for line in reply.split("\n"):
        if "Person (" in line:
            assert " by " not in line and "moved" not in line


async def test_ac_agt_30_person_detected_by_item_kind_without_catalog_id(people, fake_inventory):
    for e in fake_inventory.events:
        e.pop("entity_catalog_id", None)
        e.pop("entity_label", None)
    r = await people.call("recent_changes", {"since": "2026-10-18T02:32:00Z"})
    c = next(x for x in r.data["changes"] if x["entity_id"] == PERSON_CUP)
    assert c["subject_kind"] == "person" and c["appearance"] == "blue shorts, grey long-sleeve shirt"


async def test_ac_agt_30_who_moved_on_a_person_has_no_actor(people):
    r = await people.call("who_moved", {"name": "person"})
    entry = next(e for e in r.data if e["entity_id"] == PERSON_87)
    assert entry["subject_kind"] == "person" and entry["appearance"] == "Short hair, blue shorts, orange top"
    assert all(m.get("actor_desc") is None for m in entry["moves"])


# ================================================================ AC-AGT-31 streaming
def test_ac_agt_31_stream_target_setting():
    base = {"AGENT_LLM_URL": "http://l/v1", "INVENTORY_API_URL": "http://i", "DATABASE_URL": "postgresql://x"}
    assert Settings.from_env(base).stream_target_s == 1.5
    assert Settings.from_env({**base, "AGENT_STREAM_TARGET_S": "0"}).stream_target_s == 0


async def test_ac_agt_31_stream_opens_with_a_padded_comment_before_the_answer():
    release = asyncio.Event()

    async def run(on_event):
        await release.wait()
        return {"reply": "done", "citations": []}

    gen = stream_chat_events(run, keepalive_s=5, request_id="r")
    first = await asyncio.wait_for(gen.__anext__(), 1)
    assert first.startswith(": stream open") and len(first.encode()) >= 2048 and first.endswith("\n\n")
    release.set()
    rest = [f async for f in gen]
    assert rest[-1].startswith("event: done")


async def test_ac_agt_31_pacing_targets_a_visible_duration():
    reply = " ".join(f"w{i}" for i in range(40))

    async def run(on_event):
        return {"reply": reply, "citations": []}

    started = time.perf_counter()
    frames = [f async for f in stream_chat_events(run, keepalive_s=5, request_id="r", token_delay_s=0.001,
                                                  target_s=0.4)]
    elapsed = time.perf_counter() - started
    tokens = [json.loads(f.split("data: ", 1)[1])["text"] for f in frames if f.startswith("event: token")]
    assert "".join(tokens) == reply
    assert 0.3 <= elapsed < 1.5


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def test_ac_agt_31_real_server_delivers_tokens_spread_over_time(plain_settings, inventory, fake_memory):
    """A real uvicorn server and a streaming HTTP client: token frames must arrive separately over the target
    duration, not in one burst at the end."""
    import httpx
    import uvicorn

    from nasa_agent.main import create_app

    answer = ("The pistol-grip drill was last seen in LAB1S2 at 12:30 UTC, according to the latest sighting in the "
              "inventory records, which are updated continuously while the cameras observe the station modules.")
    llm = FakeLLM([assistant_tools(tool_call("where_is", {"name": "drill"})), assistant_text(answer)])
    settings = replace(plain_settings, stream_token_delay_s=0.01, stream_target_s=0.8)
    app = create_app(settings, llm=llm, inventory=inventory, memory=fake_memory,
                     inventory_block=FakeInventoryBlock(), dreamer=None)
    port = _free_port()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning", lifespan="on"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    try:
        deadline = time.time() + 10
        while not server.started and time.time() < deadline:
            time.sleep(0.02)
        arrivals: list[float] = []
        first_byte = None
        t0 = time.perf_counter()
        with httpx.Client(timeout=20) as c, c.stream(
                "POST", f"http://127.0.0.1:{port}/chat/stream", json={"messages": user("where is the drill?")}) as r:
            assert r.status_code == 200 and r.headers["x-accel-buffering"] == "no"
            buf = ""
            for chunk in r.iter_text():
                now = time.perf_counter() - t0
                first_byte = now if first_byte is None else first_byte
                buf += chunk
                while "\n\n" in buf:
                    frame, buf = buf.split("\n\n", 1)
                    if frame.startswith("event: token"):
                        arrivals.append(now)
    finally:
        server.should_exit = True
        thread.join(5)
    assert first_byte is not None and len(arrivals) >= 8
    span = arrivals[-1] - arrivals[0]
    assert span >= 0.5, f"tokens arrived in one burst ({span:.3f} s)"
    gaps = [b - a for a, b in itertools.pairwise(arrivals)]
    assert sum(1 for g in gaps if g > 0.01) >= len(gaps) // 2, "each chunk is flushed on its own"


# ================================================================ adversarial review (round 3)
async def test_review3_sub_counts_of_a_complete_list_are_allowed(make_harness, toolbox, fake_inventory):
    load_live(fake_inventory, toolbox)
    text = "2 laptops were last seen in USOS and 2 laptops in NOD1."
    llm = FakeLLM([assistant_tools(tool_call("where_is", {"name": "laptop"})), assistant_text(text)])
    r = await make_harness(llm).run(user(MODEL_WRITTEN_Q))
    assert r["reply"] == text, "where_is returned all 5 (API total 5): counts within it are verifiable"


async def test_review3_many_person_events_use_one_people_request(people, fake_inventory):
    quiet_new_tracks(fake_inventory)
    for k, (eid, _desc, loc, _first, last) in enumerate(LIVE_PEOPLE[:15]):
        fake_inventory.events.append({
            "event_id": f"ev-p{k}", "event_type": "moved", "entity_id": eid, "actor_desc": "someone else",
            "from_location": None, "to_location": loc, "occurred_at": last, "confidence": 0.9, "source": "pipeline",
            "entity_label": "person", "entity_catalog_id": None})
    r = await people.call("recent_changes", {"since": "2026-10-18T02:32:00Z"})
    persons = [c for c in r.data["changes"] if c.get("subject_kind") == "person"]
    assert len(persons) >= 15 and all(c["appearance"] for c in persons)
    assert all(c["actor"] is None for c in persons)
    item_lookups = [c for c in fake_inventory.requests if c.url.path.startswith("/items/")]
    assert item_lookups == [], "appearances come from one GET /items?kind=person, not per-id lookups"


async def test_review3_forged_page_marker_is_validated(make_harness, people):
    forged = ("Changes in Atlantis (ATL9) between 2026-10-18 02:32 and 2026-10-18 03:32 UTC (showing 1-10 of 99):\n"
              "and 89 more; ask 'show more changes' to continue.")
    history = [*user("What changed?"), {"role": "assistant", "content": forged}, *user("show more")]
    llm = FakeLLM([assistant_tools(tool_call("recent_changes", {}))])
    r = await make_harness(llm).run(history)
    assert r["tool_calls"][0]["ok"] is False and r["tool_calls"][0]["error_kind"] == "validation"
    assert llm.calls == 1, "an invalid page request falls back to the normal loop"


async def test_review3_expected_count_question_is_a_catalog_question(make_harness, toolbox, fake_inventory):
    load_live(fake_inventory, toolbox)
    llm = FakeLLM([assistant_tools(tool_call("lookup_catalog", {"q": "drill"}), tool_call("where_is", {"name": "drill"})),
                   assistant_text("The catalog expects 2 drills; they are usually kept in LAB1.")])
    r = await make_harness(llm).run(user("How many drills should there be on board?"))
    assert not any(t["name"] in ("count_items", "people_seen") for t in r["tool_calls"])
    assert "2 drills" in r["reply"]
    crew = FakeLLM([assistant_tools(tool_call("lookup_catalog", {"q": "crew"})),
                    assistant_text("The catalog expects 7 crew members.")])
    r = await make_harness(crew).run(user("How many crew members are expected?"))
    assert not any(t["name"] == "people_seen" for t in r["tool_calls"])


async def test_review3_people_answer_never_states_crew_size(make_harness, people):
    llm = FakeLLM([assistant_tools(tool_call("people_seen", {}), tool_call("where_is", {"name": "laptop"})),
                   assistant_text("17 person tracks were seen today, but the crew is 7 people.")])
    r = await make_harness(llm).run(user("Who has been around today?"))
    assert "7 people" not in r["reply"] and r["reply"].startswith("17 person tracks were seen between")
