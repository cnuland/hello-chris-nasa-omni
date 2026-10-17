"""End-to-end: synthetic eval scene -> perception -> inventory-api (Postgres) -> agent -> eval gate.

ACs proven across components: AC-PER-14/18 (replay end to end), AC-API-2/4/10/11/12/15/16/18/20 (what perception
wrote, read back through the API), AC-AGT-1/2/3/4 (grounded "where is the drill?" with a correct citation),
AC-AGT-6 (dreaming over the real schema), AC-EVAL-3/4/5/6 (scoring this run and gating it).
"""

from __future__ import annotations

import json
from collections import Counter
from datetime import UTC, datetime

import pytest

from conftest import tool_call, tool_results

QUESTION = "where is the drill?"


# ------------------------------------------------------------------------------------------------ helpers
def live_items(api, **params):
    page = api.get("/items", params={"limit": 500, **params})
    assert page.status_code == 200, page.text
    return page.json()["items"]


def items_by_catalog(api) -> dict[str | None, list[dict]]:
    out: dict[str | None, list[dict]] = {}
    for item in live_items(api, kind="object"):
        out.setdefault(item["catalog_id"], []).append(item)
    return out


def final_location(gold_object) -> str:
    return max(gold_object.appearances, key=lambda a: a.t).location


def where_is_policy(name: str):
    """A scripted LLM that asks ``where_is(name)`` and answers only from the tool result it got back."""

    def policy(messages, tools):
        results = tool_results(messages)
        if not results:
            assert any(t["function"]["name"] == "where_is" for t in tools)
            return tool_call("where_is", {"name": name})
        data = results[-1]["data"]
        top = data[0]
        return {
            "role": "assistant",
            "content": f"The {top['label']} is in {top['last_location']} ({top['last_location_name']}). "
            f"It was last moved by {top['last_moved_by']}.",
        }

    return policy


# ------------------------------------------------------------------------------------------------ perception -> API
def test_e2e_01_replay_posts_every_window(replay):
    """AC-PER-18: every window of the scene is reconciled and accepted by /ingest/window (AC-API-18)."""
    assert replay.frames == 60 and replay.windows > 0
    assert replay.posted == replay.windows
    assert replay.errors == 0 and replay.invalid == 0


def test_e2e_02_one_entity_per_physical_object(replay, api, dataset):
    """R4 dedup through the real API: exactly one live entity per gold object (twins stay two, the moved object
    stays one), the unknown object is an ``unknown`` entity, nothing merged or dismissed."""
    want = Counter(g.catalog_id for g in dataset.gold)
    got = {cid: len(items) for cid, items in items_by_catalog(api).items()}
    assert got == dict(want)
    unknowns = items_by_catalog(api)[None]
    assert [u["status"] for u in unknowns] == ["unknown"]
    assert live_items(api, status=["merged", "dismissed"]) == []


def test_e2e_03_drill_location_mover_and_detail(replay, api, drill, drill_move):
    """AC-API-2/4/10/11: the drill's last location is the zone it was moved to, with the mover; detail and history
    carry sightings and the move."""
    drills = live_items(api, catalog_id="drill-pistol")
    assert len(drills) == 1
    item = drills[0]
    assert item["last_location"] == final_location(drill) == drill_move.to_location
    assert item["last_moved_by"] == drill_move.actor
    assert item["snapshot_url"] and item["snapshot_url"].startswith("/media/")
    assert item["entity_id"] in {i["entity_id"] for i in live_items(api, location="LAB1")}  # descendants (AC-API-4)

    detail = api.get(f"/items/{item['entity_id']}").json()
    assert detail["exemplar_count"] >= 1 and detail["sightings"]
    assert {s["location_id"] for s in detail["sightings"]} <= {drill.appearances[0].location, final_location(drill)}
    moves = [e for e in detail["events"] if e["event_type"] == "moved"]
    assert len(moves) == 1
    history = api.get(f"/items/{item['entity_id']}/history").json()
    kinds = {h["kind"] for h in history}
    assert {"sighting", "event"} <= kinds
    assert [h["at"] for h in history] == sorted((h["at"] for h in history), reverse=True)


def test_e2e_04_events_match_gold_and_evidence_is_served(replay, api, dataset):
    """R5 / AC-API-14/20: one pipeline event per gold event (type, actor, destination); the evidence frame that
    perception uploaded is served by inventory-api /media."""
    events = api.get("/events", params={"limit": 100}).json()
    entity_catalog = {i["entity_id"]: i["catalog_id"] for i in live_items(api)}
    got = sorted(
        (e["event_type"], entity_catalog.get(e["entity_id"]), e["actor_desc"], e["to_location"], e["source"])
        for e in events
    )
    want = sorted(
        (ev.type, g.catalog_id, ev.actor, ev.to_location, "pipeline") for g in dataset.gold for ev in g.events
    )
    assert got == want
    moved = next(e for e in events if e["event_type"] == "moved")
    assert moved["from_location"] and moved["from_location"] != moved["to_location"]
    assert moved["evidence_url"] and moved["evidence_url"].startswith("/media/frames/")
    media = api.get(moved["evidence_url"])
    assert media.status_code == 200 and media.headers["content-type"] == "image/jpeg"
    assert media.content[:2] == b"\xff\xd8"


def test_e2e_05_review_queue_has_the_unknown_with_image(replay, api, dataset):
    """R6 / AC-API-12 / AC-PER-8(6): exactly one open review, an ``unknown_object`` for the unknown entity, with
    an image the dashboard can load; no spurious duplicate reviews."""
    reviews = api.get("/review").json()
    assert [r["kind"] for r in reviews] == ["unknown_object"]
    review = reviews[0]
    unknown = next(i for i in live_items(api) if i["status"] == "unknown")
    assert review["entity_id"] == unknown["entity_id"]
    assert review["location_id"] == next(g for g in dataset.gold if g.is_unknown).appearances[0].location
    image = api.get(review["image_url"])
    assert image.status_code == 200 and image.headers["content-type"] == "image/jpeg"
    assert unknown["needs_review"] is True


def test_e2e_06_search_and_stats(replay, api, dataset):
    """AC-API-15/16: transcripts are searchable as window hits; stats agree with the items."""
    hits = api.get("/search", params={"q": "inventory survey"}).json()
    assert any(h["type"] == "window" for h in hits)
    hits = api.get("/search", params={"q": "drill"}).json()
    assert any(h["type"] == "entity" for h in hits)
    stats = api.get("/stats").json()
    assert stats["by_module"].get("LAB1") == len(live_items(api))
    assert stats["open_reviews_by_kind"]["unknown_object"] == 1
    assert stats["by_status"]["unknown"] == 1


# ------------------------------------------------------------------------------------------------ agent
def test_e2e_07_agent_answers_where_is_the_drill_with_citation(replay, api, make_agent, drill, drill_move,
                                                               dataset):
    """AC-AGT-1/2/3/4: the agent calls where_is over the real inventory-api, answers with the drill's zone and
    mover, and cites the drill entity; eval's QA scorer (AC-EVAL-4) marks the answer correct for both an entity
    and a location question."""
    from nasa_eval.dataset import QAItem
    from nasa_eval.qa import score_qa_item

    client, llm = make_agent(where_is_policy("drill"))
    resp = client.post("/chat", json={"messages": [{"role": "user", "content": QUESTION}]})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    drill_id = live_items(api, catalog_id="drill-pistol")[0]["entity_id"]

    assert drill_move.to_location in body["reply"] and drill_move.actor in body["reply"]
    assert [c["entity_id"] for c in body["citations"] if c.get("entity_id")] == [drill_id]
    assert all(c.get("at") for c in body["citations"])
    assert [t["name"] for t in body["tool_calls"]] == ["where_is"]
    assert len(llm.requests) == 2 and llm.requests[0]["tool_choice"] == "auto"

    entity_to_gold = {drill_id: drill.gold_object_id}
    locations = {loc["location_id"] for loc in api.get("/locations").json()}
    for item in (
        QAItem("q-drill-entity", QUESTION, "entity", {"entities": [drill.gold_object_id]}),
        QAItem("q-drill-loc", QUESTION, "location", {"location": drill_move.to_location}),
    ):
        result = score_qa_item(item, body, entity_to_gold=entity_to_gold, location_ids=locations)
        assert result.correct, result.detail


def test_e2e_08_agent_refuses_an_ungrounded_location(replay, make_agent, drill):
    """AC-AGT-4: an LLM that answers with the drill's *old* zone (not in this turn's tool data) is overridden:
    no guess, no citations."""
    old = drill.appearances[0].location

    def policy(messages, tools):
        if not tool_results(messages):
            return tool_call("where_is", {"name": "drill"})
        return {"role": "assistant", "content": f"The drill is in {old}."}

    client, _ = make_agent(policy)
    body = client.post("/chat", json={"messages": [{"role": "user", "content": QUESTION}]}).json()
    assert old not in body["reply"] and "don't have a record" in body["reply"]
    assert body["citations"] == []


def test_e2e_09_agent_read_tools_against_real_api(replay, api, inventory_api, database_url, drill_move):
    """AC-AGT-2 contract check: every read tool works against the real inventory-api responses (the agent suite
    uses an in-memory fake of the API)."""
    import asyncio

    from nasa_agent.inventory_client import InventoryClient
    from nasa_agent.memory import MemoryStore
    from nasa_agent.tools import Toolbox

    drill_id = live_items(api, catalog_id="drill-pistol")[0]["entity_id"]

    async def run():
        inv = InventoryClient(inventory_api)
        box = Toolbox(inv, MemoryStore(database_url))
        try:
            calls = {
                "search_items": {"q": "drill"},
                "get_item": {"entity_id": drill_id},
                "item_history": {"entity_id": drill_id},
                "where_is": {"name": "drill"},
                "who_moved": {"name": "drill"},
                "list_unknowns": {"location": "LAB1"},
                "inventory_stats": {},
                "search_observations": {"q": "inventory survey"},
            }
            return {name: await box.call(name, args) for name, args in calls.items()}
        finally:
            await inv.aclose()

    results = asyncio.run(run())
    for name, res in results.items():
        assert res.ok and res.has_data, (name, res.error)
    moves = results["who_moved"].data[0]["moves"]
    assert [m["actor_desc"] for m in moves] == [drill_move.actor]
    assert len(results["list_unknowns"].data) == 1
    assert results["search_observations"].citations and "window_id" in results["search_observations"].citations[0]


def test_e2e_10_dreaming_over_the_real_schema(replay, make_agent, database_url):
    """AC-AGT-6: dreaming builds global/location/entity summaries from what perception wrote; it is idempotent."""
    import psycopg

    client, _ = make_agent(lambda m, t: {"role": "assistant", "content": "unused"})
    first = client.post("/memory/dream", json={"hours": 24 * 365 * 5})
    assert first.status_code == 200, first.text
    with psycopg.connect(database_url) as conn:
        rows = conn.execute("SELECT scope, scope_key, summary FROM agent_memory ORDER BY scope, scope_key").fetchall()
    scopes = Counter(r[0] for r in rows)
    assert scopes["global"] == 1 and scopes["location"] >= 1 and scopes["entity"] >= 1
    assert client.post("/memory/dream", json={"hours": 24 * 365 * 5}).status_code == 200
    with psycopg.connect(database_url) as conn:
        again = conn.execute("SELECT scope, scope_key, summary FROM agent_memory ORDER BY scope, scope_key").fetchall()
    assert [r[:2] for r in again] == [r[:2] for r in rows]


# ------------------------------------------------------------------------------------------------ eval gate
def test_e2e_11_eval_scores_this_run_and_the_gate_passes(replay, api, make_agent, database_url, dataset, tmp_path):
    """AC-EVAL-3/5/6: the run in Postgres is scored against the gold of the scene, written as a pipeline report
    (with a QA section from the agent) and ``nasa_eval gate`` exits 0 on it."""
    from nasa_eval import THRESHOLDS_PATH
    from nasa_eval.dataset import QAItem
    from nasa_eval.gate import check, load_thresholds, run_gate
    from nasa_eval.qa import score_qa
    from nasa_eval.runner import actor_descriptors, build_report, write_report
    from nasa_eval.scoring import score
    from nasa_eval.snapshot import read_snapshot

    snapshot = read_snapshot(database_url)
    result = score(dataset, snapshot)
    drill = next(g for g in dataset.gold if g.catalog_id == "drill-pistol")
    move = next(e for e in drill.events if e.type == "moved")
    qa_items = [
        QAItem("q-drill-loc", QUESTION, "location", {"location": move.to_location}),
        QAItem("q-drill-entity", QUESTION, "entity", {"entities": [drill.gold_object_id]}),
    ]
    client, _ = make_agent(where_is_policy("drill"))
    reply = client.post("/chat", json={"messages": [{"role": "user", "content": QUESTION}]}).json()
    qa = score_qa(
        qa_items,
        {q.qa_id: reply for q in qa_items},
        entity_to_gold=result["entity_to_gold"],
        location_ids=set(snapshot.locations),
        actor_descriptors=actor_descriptors(dataset),
        dataset_name=dataset.name,
        reports_dir=tmp_path / "qa",
    )
    assert qa["accuracy"] == pytest.approx(1.0)

    now = datetime.now(UTC)
    report = build_report(dataset, result, now=now, source="replay", target="local", database=None, stats=replay,
                          extra={"qa": {"accuracy": qa["accuracy"], "n": qa["n"]}})
    json_path, md_path = write_report(report, tmp_path / "reports", now, dataset.name)
    assert json.loads(json_path.read_text())["kind"] == "pipeline" and md_path.exists()

    gate = check(result["metrics"], load_thresholds(THRESHOLDS_PATH))
    assert gate.passed, gate.failures
    # gold-scripted fakes on a deterministic scene: entity resolution must be exact, not just above the gates
    # (false_merge_rate is not gated in eval/thresholds.yaml; a twin swap would otherwise pass unnoticed)
    m = result["metrics"]
    assert m["duplicate_rate"] == 0.0 and m["false_merge_rate"] == 0.0 and m["spurious_entity_rate"] == 0.0
    assert m["pairwise_f1"] == 1.0 and m["location_acc"] == 1.0 and m["unknown_recall"] == 1.0
    assert m["event_f1"] == 1.0 and m["actor_accuracy"] == 1.0
    assert run_gate(reports_dir=tmp_path / "reports") == 0
