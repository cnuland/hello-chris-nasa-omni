"""UI review round 5 (docs/ui-review/round5): AC-AGT-36 "who was in X" means any sighting there (UI5-04), AC-AGT-37
"show more" finds the latest list and pages with a frozen cursor (UI5-07), AC-AGT-38 honest empty answers and
rendered-row citations (UI5-08), AC-AGT-39 an item never seen gets its catalog entry (UI5-08 drill)."""
from __future__ import annotations

from datetime import timedelta

import pytest

from nasa_agent import tools as tools_mod
from tests.fakes import FakeLLM, _item, assistant_text, assistant_tools, tool_call
from tests.live_round4 import _event
from tests.live_round5 import CUPOLA_VISITORS, NOW, UNKNOWNS_5, add_hour_of_changes, load_round5


@pytest.fixture()
def r5(toolbox, fake_inventory):
    load_round5(fake_inventory, toolbox)
    return toolbox


def args_of(res, name):
    return [t["arguments"] for t in res["tool_calls"] if t["name"] == name]


def detail_calls(fake_inventory):
    return [r for r in fake_inventory.requests if r.url.path.startswith("/items/")]


def turns(*pairs, question):
    out = []
    for q, a in pairs:
        out += [{"role": "user", "content": q}, {"role": "assistant", "content": a}]
    return out + [{"role": "user", "content": question}]


# ================================================================ AC-AGT-36 who WAS in X vs who IS in X now
async def test_ac_agt_36_visited_counts_any_sighting_in_the_place(r5, fake_inventory):
    """UI5-04: tracks whose last location fell back to USOS but were sighted in CUP1 are found."""
    r = await r5.call("people_seen", {"location": "Cupola"})
    assert r.ok, r.error
    assert r.data["mode"] == "visited" and r.data["location"] == "CUP1"
    assert [t["entity_id"] for t in r.data["tracks"]] == CUPOLA_VISITORS
    assert r.data["total_tracks"] == 4 and r.data["complete"] is True
    t395 = next(t for t in r.data["tracks"] if t["entity_id"].startswith("395bae46"))
    assert t395["location"] == "CUP1" and t395["seen_here_at"].startswith("2026-10-18T06:31:08")
    assert t395["last_location"] == "USOS"
    assert "location" not in fake_inventory.params(fake_inventory.calls("/items")[-1]), "not a last_location filter"
    cites = {c["entity_id"]: c for c in r.citations}
    assert set(cites) == set(CUPOLA_VISITORS)
    assert cites[t395["entity_id"]]["location"] == "CUP1" and cites[t395["entity_id"]]["at"].startswith(
        "2026-10-18T06:31:08"), "cited at the Cupola sighting, not the last one"


async def test_ac_agt_36_now_mode_uses_last_location(r5, fake_inventory):
    r = await r5.call("people_seen", {"location": "CUP1", "mode": "now"})
    assert r.ok and r.data["mode"] == "now" and r.data["total_tracks"] == 0 and r.data["tracks"] == []
    assert fake_inventory.params(fake_inventory.calls("/items")[-1])["location"] == ["CUP1"]
    assert not detail_calls(fake_inventory)


async def test_ac_agt_36_window_bounds_the_sightings(r5):
    """A sighting before `since` or at/after `until` does not count."""
    r = await r5.call("people_seen", {"location": "CUP1", "since": "2026-10-18T06:32:00Z",
                                      "until": "2026-10-18T06:46:30Z"})
    assert r.ok and r.data["total_tracks"] == 1
    assert r.data["tracks"][0]["entity_id"].startswith("f77dc775")  # its 06:46:08 sighting
    assert r.data["tracks"][0]["seen_here_at"].startswith("2026-10-18T06:46:08")


async def test_ac_agt_36_track_last_seen_inside_needs_no_lookup(r5, fake_inventory):
    p = next(i for i in fake_inventory.items if i["entity_id"].startswith("8c58d341"))  # ZVEZDA, one sighting
    r = await r5.call("people_seen", {"location": "ZVEZDA"})
    ids = [t["entity_id"] for t in r.data["tracks"]]
    assert p["entity_id"] in ids and any(i.startswith("395bae46") for i in ids)  # 395bae46 via its sighting
    looked_up = {r.url.path.rsplit("/", 1)[-1] for r in detail_calls(fake_inventory)}
    assert p["entity_id"] not in looked_up and "34eb4266-973c-42e1-aab8-c0e54d86c762" not in looked_up


async def test_ac_agt_36_lookups_are_bounded_and_unchecked_tracks_reported(r5, fake_inventory, monkeypatch):
    monkeypatch.setattr(tools_mod, "MAX_PRESENCE_LOOKUPS", 2)
    r = await r5.call("people_seen", {"location": "CUP1"})
    assert len(detail_calls(fake_inventory)) == 2
    assert r.data["unchecked"] == 12 and r.data["complete"] is False
    assert r.data["total_tracks"] == len(r.data["tracks"]) <= 2, "unchecked tracks are never counted as visitors"


async def test_ac_agt_36_capped_detail_without_a_match_is_unchecked(r5, fake_inventory):
    """GET /items/{id} returns at most 20 sightings; a track whose 20 returned sightings are all newer than `since`
    and elsewhere may still have an older Cupola sighting, so it is not silently ruled out."""
    eid = "1ea5e31f-2d43-450c-8012-7ed9f242f756"
    fake_inventory.sightings[eid] = [{"location_id": "USOS", "seen_at": f"2026-10-18T06:{40 - k:02d}:00Z"}
                                     for k in range(25)]
    r = await r5.call("people_seen", {"location": "CUP1"})
    assert r.data["unchecked"] == 1 and r.data["total_tracks"] == 4 and r.data["complete"] is False


async def test_ac_agt_36_failed_detail_is_unchecked_not_absent(r5, fake_inventory):
    eid = "395bae46-07da-4289-9f11-8fc40b39cd7f"
    fake_inventory.fail_paths = {f"/items/{eid}"}
    r = await r5.call("people_seen", {"location": "CUP1"})
    assert r.ok and eid not in {t["entity_id"] for t in r.data["tracks"]} and r.data["unchecked"] == 1


async def test_ac_agt_36_who_was_in_the_cupola_routed_and_rendered(r5, make_harness):
    llm = FakeLLM([])
    res = await make_harness(llm).run([{"role": "user", "content": "Who was in the Cupola?"}])
    assert llm.calls == 0
    assert args_of(res, "people_seen") == [{"location": "CUP1"}]
    reply = res["reply"]
    assert reply.startswith("4 person tracks were seen in Cupola (CUP1) between 2026-10-18 00:00 and"), reply
    line = next(x for x in reply.splitlines() if "grey long-sleeve shirt" in x)
    assert "seen in Cupola (CUP1) at 2026-10-18 06:31 UTC" in line
    assert "last seen in US Orbital Segment (USOS)" in line and "06:47 UTC" in line
    assert [c["entity_id"] for c in res["citations"]] == CUPOLA_VISITORS


@pytest.mark.parametrize("question", ["Who is in the Cupola now?", "Who is in the Cupola right now?",
                                      "Who is currently in the Cupola?"])
async def test_ac_agt_36_who_is_in_x_now_uses_last_location(r5, make_harness, question):
    llm = FakeLLM([])
    res = await make_harness(llm).run([{"role": "user", "content": question}])
    assert llm.calls == 0
    assert args_of(res, "people_seen") == [{"location": "CUP1", "mode": "now"}]
    assert "latest sighting" in res["reply"] and res["reply"].startswith("No person track"), res["reply"]
    assert "Who was in" in res["reply"], "points to the sighting question"
    assert res["citations"] == []


async def test_ac_agt_36_now_mode_lists_tracks_last_seen_there(r5, make_harness):
    llm = FakeLLM([])
    res = await make_harness(llm).run([{"role": "user", "content": "Who is in Zvezda now?"}])
    assert res["reply"].startswith("2 person tracks were last seen in Zvezda Service Module (ZVEZDA)"), res["reply"]
    assert "their latest sighting is there" in res["reply"]


async def test_ac_agt_36_model_mode_is_set_from_the_question(r5, make_harness):
    """The model path: the harness decides visited/now from the current message, not the model."""
    q = "Which crew members are in the Cupola right now, and what are they wearing?"
    llm = FakeLLM([assistant_tools(tool_call("people_seen", {"location": "CUP1"}))])
    res = await make_harness(llm).run([{"role": "user", "content": q}])
    assert args_of(res, "people_seen") == [{"location": "CUP1", "mode": "now"}]
    q2 = "Which crew members were in the Cupola, and what were they wearing?"
    llm2 = FakeLLM([assistant_tools(tool_call("people_seen", {"location": "CUP1", "mode": "now"}))])
    res2 = await make_harness(llm2).run([{"role": "user", "content": q2}])
    assert args_of(res2, "people_seen") == [{"location": "CUP1"}]


# ================================================================ AC-AGT-37 show more: latest list, frozen cursor
CHANGES_Q = "What changed in the last hour?"
OTHER_TURNS = [("Where is the multimeter?", "The Fluke multimeter was last seen in Destiny (US Lab) (LAB1)."),
               ("Who was in the Cupola?", "4 person tracks were seen in Cupola (CUP1)."),
               ("Any unknown objects in Node 1?", "There are 2 unknown objects in Node 1 Unity (NOD1).")]


async def _page_one(r5, fake_inventory, harness):
    add_hour_of_changes(fake_inventory)
    first = await harness.run([{"role": "user", "content": CHANGES_Q}], session_id="s1")
    assert "(showing 1-10 of 59)" in first["reply"], first["reply"]
    assert "Item 10 moved" in first["reply"] and "Item 11" not in first["reply"]
    return first


async def test_ac_agt_37_show_more_after_other_questions_is_code_paged(r5, fake_inventory, make_harness):
    """UI5-07: 'show more changes' three turns after the list: no model call, rows 11-20 exactly."""
    llm = FakeLLM([])
    harness = make_harness(llm)
    first = await _page_one(r5, fake_inventory, harness)
    msgs = turns((CHANGES_Q, first["reply"]), *OTHER_TURNS, question="show more changes")
    res = await harness.run(msgs, session_id="s1")
    assert llm.calls == 0
    assert "(showing 11-20 of 59)" in res["reply"], res["reply"]
    assert "Item 11 moved" in res["reply"] and "Item 20 moved" in res["reply"]
    assert "Item 10 " not in res["reply"] and "Item 21" not in res["reply"]


async def test_ac_agt_37_new_events_do_not_shift_page_two(r5, fake_inventory, make_harness):
    """New events after page 1 (a later one and a late-ingested one inside the window) neither repeat nor shift rows;
    the clock moving on does not move the window."""
    llm = FakeLLM([])
    harness = make_harness(llm)
    first = await _page_one(r5, fake_inventory, harness)
    later = NOW + timedelta(minutes=2)
    r5.now = lambda: later
    fake_inventory.events += [
        _event("7d7d7d7d-0000-4000-8000-000000000001", "moved", "3735b3dc-0d6a-4c1e-9a43-0f7e1a3b5c01",
               (NOW + timedelta(seconds=40)).strftime("%Y-%m-%dT%H:%M:%SZ"), label="late one", frm="USOS"),
        _event("7d7d7d7d-0000-4000-8000-000000000002", "moved", "3735b3dc-0d6a-4c1e-9a43-0f7e1a3b5c01",
               (NOW - timedelta(seconds=45)).strftime("%Y-%m-%dT%H:%M:%SZ"), label="backdated one", frm="USOS"),
    ]
    res = await harness.run(turns((CHANGES_Q, first["reply"]), *OTHER_TURNS, question="show more changes"),
                            session_id="s1")
    assert llm.calls == 0
    reply = res["reply"]
    assert "(showing 11-20 of 59)" in reply, reply
    assert "\n11. " in reply and "\n10. " not in reply
    assert "Item 11 moved" in reply and "Item 10 " not in reply and "Item 09" not in reply
    assert "late one" not in reply.lower() and "backdated one" not in reply.lower()
    call = args_of(res, "recent_changes")[-1]
    assert call["until"] == "2026-10-18T06:50:00Z", "page 1's exact end, not the clock"
    assert call.get("cursor", "").endswith("|e:7c7c7c7c-0000-4000-8000-000000000009"), call


async def test_ac_agt_37_page_three_continues_from_page_two(r5, fake_inventory, make_harness):
    llm = FakeLLM([])
    harness = make_harness(llm)
    first = await _page_one(r5, fake_inventory, harness)
    second = await harness.run(turns((CHANGES_Q, first["reply"]), question="show more changes"), session_id="s1")
    third = await harness.run(turns((CHANGES_Q, first["reply"]), ("show more changes", second["reply"]),
                                    question="more"), session_id="s1")
    assert "(showing 21-30 of 59)" in third["reply"] and "Item 21 moved" in third["reply"]
    assert llm.calls == 0


async def test_ac_agt_37_unknown_list_falls_back_to_the_text_marker(r5, fake_inventory, make_harness):
    """After an agent restart the cursor is gone: the window and range are read back from the reply text."""
    llm = FakeLLM([])
    first = await _page_one(r5, fake_inventory, make_harness(llm))
    res = await make_harness(llm).run(turns((CHANGES_Q, first["reply"]), *OTHER_TURNS, question="show more changes"))
    assert llm.calls == 0 and "(showing 11-" in res["reply"]


async def test_ac_agt_37_bare_more_only_pages_the_previous_reply(r5, fake_inventory, make_harness):
    """'more' (no list type) after an unrelated answer does not page an older list; the model answers."""
    llm = FakeLLM([], default=assistant_text("Could you say what you would like more of?"))
    harness = make_harness(llm)
    first = await _page_one(r5, fake_inventory, harness)
    res = await harness.run(turns((CHANGES_Q, first["reply"]), *OTHER_TURNS, question="more"), session_id="s1")
    assert llm.calls >= 1 and "(showing 11-" not in res["reply"]
    assert not args_of(res, "recent_changes")


async def test_ac_agt_37_typed_show_more_picks_the_matching_list(r5, fake_inventory, make_harness):
    """A later people list does not capture 'show more changes'."""
    llm = FakeLLM([])
    harness = make_harness(llm)
    first = await _page_one(r5, fake_inventory, harness)
    people = ("Who was seen today?", ("14 person tracks were seen between 2026-10-18 00:00 and 2026-10-18 06:50 UTC."
              "\n\nMost recent tracks (showing 1-10 of 14):\n1. Person\n\nand 4 more; ask 'show more people' to "
              "continue."))
    res = await harness.run(turns((CHANGES_Q, first["reply"]), people, question="show more changes"),
                            session_id="s1")
    assert llm.calls == 0 and "(showing 11-20 of 59)" in res["reply"]


async def test_ac_agt_37_cursor_is_harness_only(r5):
    spec = {t["function"]["name"]: t["function"] for t in r5.specs()}["recent_changes"]
    assert "cursor" not in spec["parameters"]["properties"]
    bad = await r5.call("recent_changes", {"cursor": "yesterday|e:1"})
    assert not bad.ok and bad.error_kind == "validation"
    bad2 = await r5.call("recent_changes", {"cursor": "2026-13-45T99:00:00Z|e:1"})
    assert not bad2.ok and bad2.error_kind == "validation"


async def test_ac_agt_37_cursor_pages_by_key_not_offset(r5, fake_inventory):
    ids = add_hour_of_changes(fake_inventory, 15)
    page = await r5.call("recent_changes", {"since": "2026-10-18T05:50:00Z", "until": "2026-10-18T06:50:00Z"})
    tenth = page.data["changes"][9]
    assert tenth["event_id"] == ids[9]
    cursor = tools_mod.change_cursor(tenth)
    # a backdated row newer than the cursor arrives: offset paging would shift, key-set paging does not
    fake_inventory.events.append(_event("7d7d7d7d-0000-4000-8000-0000000000aa", "used",
                                        "3735b3dc-0d6a-4c1e-9a43-0f7e1a3b5c01", "2026-10-18T06:49:00Z",
                                        label="backdated", frm="USOS"))
    nxt = await r5.call("recent_changes", {"since": "2026-10-18T05:50:00Z", "until": "2026-10-18T06:50:00Z",
                                           "offset": 10, "cursor": cursor})
    assert [c["event_id"] for c in nxt.data["changes"]] == ids[10:15]
    assert nxt.data["total"] == 15 and nxt.data["offset"] == 10


# ================================================================ AC-AGT-38 honest empty answers
async def test_ac_agt_38_crew_quarters_answer_names_what_was_checked(r5, make_harness):
    """UI5-08: no model call, no unrelated citations, the private-zone note."""
    llm = FakeLLM([])
    res = await make_harness(llm).run([{"role": "user", "content": "What is in the crew quarters?"}])
    assert llm.calls == 0
    reply = res["reply"]
    assert reply.startswith("I checked inventory items in Node 2 crew quarters (NOD2D5, NOD2O5, NOD2P5, NOD2S5): "
                            "none are recorded."), reply
    assert "Crew quarters are private" in reply and "no images" in reply
    assert res["citations"] == []


async def test_ac_agt_38_private_zone_items_are_listed_without_descriptions(r5, fake_inventory, make_harness):
    item = _item("9a9a9a9a-0000-4000-8000-000000000001", "laptop", last_location="NOD2S5",
                 last_seen_at="2026-10-18T06:10:00Z", description="open on the sleeping bag")
    fake_inventory.items.append(item)
    res = await make_harness(FakeLLM([])).run([{"role": "user", "content": "What's in crew quarters?"}])
    assert "1 inventory item is recorded" in res["reply"] and "Laptop" in res["reply"], res["reply"]
    assert "sleeping bag" not in res["reply"]
    assert [c["entity_id"] for c in res["citations"]] == [item["entity_id"]]


async def test_ac_agt_38_toilet_is_private_too(r5, make_harness):
    res = await make_harness(FakeLLM([])).run([{"role": "user", "content": "What is in the toilet?"}])
    assert res["reply"].startswith("I checked inventory items in Node 3 WHC (toilet, NOD3F4): none are recorded.")
    assert "private" in res["reply"]


async def test_ac_agt_38_model_reply_cites_only_rows_it_names(r5, make_harness):
    """A list_unknowns result the reply does not use is not cited."""
    q = "Is anything odd lying around the galley that we should know about?"
    llm = FakeLLM([assistant_tools(tool_call("list_unknowns", {})),
                   assistant_text("I do not have a record of any specific items in the galley.")])
    res = await make_harness(llm).run([{"role": "user", "content": q}])
    assert res["citations"] == [], res["citations"]
    llm2 = FakeLLM([assistant_tools(tool_call("list_unknowns", {})),
                    assistant_text("There is an unknown yellow latch in Node 1 Unity (NOD1).")])
    res2 = await make_harness(llm2).run([{"role": "user", "content": q}])
    assert [c["entity_id"] for c in res2["citations"]] == [UNKNOWNS_5[0][0]], res2["citations"]


async def test_ac_agt_38_empty_lookup_names_what_was_checked(r5, make_harness):
    llm = FakeLLM([assistant_tools(tool_call("search_items", {"q": "sextant"})),
                   assistant_text("I don't have a record of that.")])
    res = await make_harness(llm).run([{"role": "user", "content": "Do we have a sextant somewhere?"}])
    assert 'I checked items matching "sextant": none are recorded' in res["reply"], res["reply"]
    assert "No sextant has been seen yet in this run" in res["reply"]
    assert "catalog has no entry" in res["reply"]
    assert res["citations"] == []


# ================================================================ AC-AGT-39 never-seen item: the catalog entry
async def test_ac_agt_39_who_used_the_drill_with_no_drill_seen(r5, make_harness):
    llm = FakeLLM([assistant_tools(tool_call("who_moved", {"name": "drill"})),
                   assistant_text("I don't have a record of that.")])
    res = await make_harness(llm).run([{"role": "user", "content": "Who used the drill?"}])
    assert res["reply"].startswith("No drill has been seen yet in this run (catalog: Pistol-grip drill, usually kept "
                                   "in Destiny (US Lab, LAB1); 2 expected on board)."), res["reply"]
    assert [c.get("catalog_id") for c in res["citations"]] == ["drill-pistol"]


async def test_ac_agt_39_drill_seen_but_never_handled(r5, fake_inventory, make_harness):
    drill = _item("9b9b9b9b-0000-4000-8000-000000000001", "pistol-grip drill", catalog_id="drill-pistol",
                  catalog_name="Pistol-grip drill", last_location="LAB1", last_seen_at="2026-10-18T06:20:00Z")
    fake_inventory.items.append(drill)
    llm = FakeLLM([assistant_tools(tool_call("who_moved", {"name": "drill"})),
                   assistant_text("I don't have a record of that.")])
    res = await make_harness(llm).run([{"role": "user", "content": "Who used the drill?"}])
    assert res["reply"].startswith('I checked moves, uses and hand-overs of 1 item matching "drill": none are '
                                   "recorded."), res["reply"]
    assert "Pistol-grip drill: last seen in Destiny (US Lab, LAB1) at 2026-10-18 06:20 UTC" in res["reply"]
    assert [c["entity_id"] for c in res["citations"]] == [drill["entity_id"]]


# ================================================================ adversarial review (round 5)
async def test_review_who_was_in_crew_quarters_is_not_reported(r5, make_harness):
    llm = FakeLLM([])
    res = await make_harness(llm).run([{"role": "user", "content": "Who was in the crew quarters?"}])
    assert llm.calls == 0 and res["tool_calls"] == [] and res["citations"] == []
    assert res["reply"].startswith("Crew quarters are private") and "I don't report who was in a private area." in \
        res["reply"]


@pytest.mark.parametrize("question", ["What changed in the crew quarters?", "Is the drill in the crew quarters?"])
async def test_review_other_private_questions_take_the_normal_path(r5, make_harness, question):
    llm = FakeLLM([], default=assistant_text("I don't have a record of that."))
    res = await make_harness(llm).run([{"role": "user", "content": question}])
    assert not any(t["name"] == "private_area_items" for t in res["tool_calls"])


async def test_review_private_scope_terms_and_module_narrowing(r5):
    cq = await r5.private_places("anything in the CQ in Node 2?")
    assert cq is not None and cq.locations == ("NOD2D5", "NOD2O5", "NOD2P5", "NOD2S5") and cq.crew_quarters
    assert "node" not in cq.rest.lower() and "cq" not in cq.rest.lower()
    whc = await r5.private_places("what's in the WHC")
    assert whc is not None and whc.locations == ("NOD3F4",) and not whc.crew_quarters
    assert await r5.private_places("what is in the cq of this question") is None, "'cq' lower case is not CQ"
    assert await r5.private_places("What is in Node 2?") is None


async def test_review_who_moved_since_is_stated(r5, fake_inventory, make_harness):
    fake_inventory.items.append(_item("9b9b9b9b-0000-4000-8000-000000000002", "pistol-grip drill",
                                      catalog_id="drill-pistol", last_location="LAB1",
                                      last_seen_at="2026-10-18T06:20:00Z"))
    llm = FakeLLM([assistant_tools(tool_call("who_moved", {"name": "the drill", "since": "2026-10-18T06:00:00Z",
                                                           "event_types": ["used"]})),
                   assistant_text("I don't have a record of that.")])
    res = await make_harness(llm).run([{"role": "user", "content": "Who used the drill this morning?"}])
    assert res["reply"].startswith('I checked events (used) of 1 item matching "drill" since 2026-10-18 06:00 UTC: '
                                   "none are recorded."), res["reply"]


async def test_review_scope_with_parenthesised_name_is_read_back(make_harness, toolbox, fake_inventory):
    """A list scoped to "Destiny (US Lab, LAB1)" keeps its place when paged from the reply text."""
    from nasa_agent.render import parse_page_marker
    reply = ("Changes in Destiny (US Lab, LAB1) between 2026-10-18 02:20 and 2026-10-18 03:20 UTC (showing 1-10 of 52):"
             "\n1. x\n\nand 42 more; ask 'show more changes' to continue.")
    marker = parse_page_marker(reply, "show more changes")
    assert marker is not None and marker["location"] == "LAB1"


async def test_review_cursor_is_per_session(r5, fake_inventory, make_harness):
    """Another session's page cursor is never used; that session falls back to the reply text."""
    llm = FakeLLM([])
    harness = make_harness(llm)
    first = await _page_one(r5, fake_inventory, harness)
    res = await harness.run(turns((CHANGES_Q, first["reply"]), question="show more changes"), session_id="other")
    assert "cursor" not in args_of(res, "recent_changes")[-1] and "(showing 11-" in res["reply"]


async def test_review_now_question_words():
    from nasa_agent.render import is_now_question
    assert is_now_question("Who is in the Cupola?") and is_now_question("who's in node 1")
    assert is_now_question("Is anyone in the lab right now?")
    assert not is_now_question("Who was in the Cupola?") and not is_now_question("Who has been in the Cupola today?")
    assert not is_now_question("Who is seen in the Cupola today?")
