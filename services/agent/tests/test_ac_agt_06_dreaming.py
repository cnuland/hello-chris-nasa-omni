"""AC-AGT-6 dreaming: ``POST /memory/dream`` + a periodic job build per-entity, per-location (module) and global
summaries from the last N hours of observation windows, events and current inventory into ``agent_memory`` (upsert).
Inventory facts (current location, status, unresolved reviews) come first; chatter is truncated to the token budget.
Idempotent.

Interface: ``Dreamer(database_url, token_budget=400).run(hours, now)`` -> {"entities", "locations", "global"};
``dream_forever(dreamer, interval_s, hours, stop)``; CLI ``nasa_agent.dreaming.main(["--once", "--hours", "6"])``.
Location scope keys are module ids (camera -> module, zone -> parent module).
"""
from __future__ import annotations

import asyncio
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient

from nasa_agent.context import estimate_tokens
from nasa_agent.dreaming import Dreamer, dream_forever, main
from tests.conftest import (
    make_entity,
    make_event,
    make_review,
    make_sighting,
    make_window,
    memory_rows,
    table_counts,
    ts,
)
from tests.fakes import FakeInventoryBlock, FakeLLM, FakeMemory

NOW = ts(60)


@pytest.fixture()
def world(db):
    w = {}
    w["drill"] = make_entity(db, label="pistol-grip drill", catalog_id="drill-pistol", status="confirmed",
                             last_location="LAB1S2", last_seen_at=ts(50))
    w["w_drill"] = make_window(db, camera_id="cam-lab1-fwd", t_start=ts(45),
                               scene_summary="Crew member stows the drill in rack S2",
                               transcript="DRILLCHATTER putting the drill back in the starboard rack")
    make_sighting(db, w["drill"], seen_at=ts(45), window_id=w["w_drill"], location_id="LAB1S2")
    make_event(db, w["drill"], occurred_at=ts(44), actor_desc="crew member in blue shirt", from_location="LAB1P3",
               to_location="LAB1S2")

    w["pouch"] = make_entity(db, label="unknown white pouch", status="unknown", last_location="NOD2O4",
                             last_seen_at=ts(30), needs_review=True)
    w["w_pouch"] = make_window(db, camera_id="cam-nod2-aft", t_start=ts(30),
                               scene_summary="POUCHCHATTER white pouch floating near overhead bay")
    make_sighting(db, w["pouch"], seen_at=ts(30), window_id=w["w_pouch"], location_id="NOD2O4")
    w["review"] = make_review(db, entity_id=w["pouch"], description="white pouch with velcro", location_id="NOD2O4",
                              observed_at=ts(30))

    w["wrench"] = make_entity(db, label="wrench", catalog_id="wrench", last_location="NOD1D1",
                              last_seen_at=ts(-60 * 24))
    w["w_old"] = make_window(db, camera_id="cam-handheld", t_start=ts(-60 * 24),
                             scene_summary="OLDCHATTER wrench floating in node 1")
    make_sighting(db, w["wrench"], seen_at=ts(-60 * 24), window_id=w["w_old"], location_id="NOD1D1")

    w["dup"] = make_entity(db, label="drill duplicate", status="merged", merged_into=w["drill"],
                           last_location="LAB1S2", last_seen_at=ts(45))
    make_sighting(db, w["dup"], seen_at=ts(45), window_id=w["w_drill"], location_id="LAB1S2")
    return w


@pytest.fixture()
def dreamer(database_url):
    return Dreamer(database_url, token_budget=400)


def first_index(text: str, needles) -> int:
    idx = [text.find(n) for n in needles if text.find(n) >= 0]
    return min(idx) if idx else -1


# ---------------------------------------------------------------- what gets written
def test_ac_agt_06_writes_entity_location_and_global_rows(dreamer, world, db):
    report = dreamer.run(hours=6, now=NOW)
    rows = memory_rows(db)
    assert ("entity", world["drill"]) in rows
    assert ("entity", world["pouch"]) in rows
    assert ("location", "LAB1") in rows and ("location", "NOD2") in rows
    assert ("global", "global") in rows
    assert report["global"] == 1
    assert report["entities"] == sum(1 for k in rows if k[0] == "entity")
    assert report["locations"] == sum(1 for k in rows if k[0] == "location")


def test_ac_agt_06_merged_entities_get_no_summary(dreamer, world, db):
    dreamer.run(hours=6, now=NOW)
    assert ("entity", world["dup"]) not in memory_rows(db)


def test_ac_agt_06_only_last_n_hours_of_chatter(dreamer, world, db):
    dreamer.run(hours=6, now=NOW)
    for row in memory_rows(db).values():
        assert "OLDCHATTER" not in row["summary"]


def test_ac_agt_06_source_window_recorded(dreamer, world, db):
    dreamer.run(hours=6, now=NOW)
    row = memory_rows(db)[("global", "global")]
    assert row["source_to"] == NOW
    assert row["source_from"] == NOW - timedelta(hours=6)


# ---------------------------------------------------------------- inventory facts first
def test_ac_agt_06_entity_summary_inventory_facts_first(dreamer, world, db):
    dreamer.run(hours=6, now=NOW)
    s = memory_rows(db)[("entity", world["drill"])]["summary"]
    assert "LAB1S2" in s and "confirmed" in s
    assert "DRILLCHATTER" in s or "stows" in s, "recent chatter should be summarised when it fits"
    chatter_at = first_index(s, ["DRILLCHATTER", "stows"])
    assert s.find("LAB1S2") < chatter_at and s.find("confirmed") < chatter_at


def test_ac_agt_06_entity_summary_mentions_unresolved_review_before_chatter(dreamer, world, db):
    dreamer.run(hours=6, now=NOW)
    s = memory_rows(db)[("entity", world["pouch"])]["summary"]
    low = s.lower()
    assert "unknown" in low and "NOD2O4" in s and "review" in low
    chatter_at = s.find("POUCHCHATTER")
    if chatter_at >= 0:
        assert low.find("review") < chatter_at


def test_ac_agt_06_location_summary_lists_module_inventory(dreamer, world, db):
    dreamer.run(hours=6, now=NOW)
    rows = memory_rows(db)
    assert "pistol-grip drill" in rows[("location", "LAB1")]["summary"]
    assert "unknown white pouch" in rows[("location", "NOD2")]["summary"]
    assert "pistol-grip drill" not in rows[("location", "NOD2")]["summary"]


def test_ac_agt_06_global_summary_lists_unresolved_reviews(dreamer, world, db):
    dreamer.run(hours=6, now=NOW)
    g = memory_rows(db)[("global", "global")]["summary"]
    assert "unknown white pouch" in g
    assert "review" in g.lower()


def test_ac_agt_06_inventory_rows_have_higher_priority_than_chatter_only(dreamer, world, db):
    dreamer.run(hours=6, now=NOW)
    rows = memory_rows(db)
    for row in rows.values():
        assert 0.0 <= row["priority"] <= 1.0
    assert rows[("entity", world["pouch"])]["priority"] >= rows[("entity", world["drill"])]["priority"], \
        "an entity with an unresolved review is at least as important as a settled one"


# ---------------------------------------------------------------- budget / truncation
def test_ac_agt_06_chatter_truncated_to_budget_facts_kept(database_url, world, db):
    w = make_window(db, camera_id="cam-lab1-fwd", t_start=ts(55), transcript="NOISE " + "blah " * 8000)
    make_sighting(db, world["drill"], seen_at=ts(55), window_id=w, location_id="LAB1S2")
    budget = 120
    Dreamer(database_url, token_budget=budget).run(hours=6, now=NOW)
    rows = memory_rows(db)
    for key, row in rows.items():
        assert estimate_tokens(row["summary"]) <= budget, key
    s = rows[("entity", world["drill"])]["summary"]
    assert "LAB1S2" in s and "confirmed" in s


# ---------------------------------------------------------------- idempotency / upsert
def test_ac_agt_06_idempotent(dreamer, world, db):
    dreamer.run(hours=6, now=NOW)
    first = memory_rows(db)
    dreamer.run(hours=6, now=NOW)
    assert memory_rows(db) == first


def test_ac_agt_06_upsert_refreshes_changed_inventory(dreamer, world, db):
    dreamer.run(hours=6, now=NOW)
    n = len(memory_rows(db))
    db.execute("UPDATE entity SET last_location='NOD2D4', last_seen_at=%s WHERE entity_id=%s", (ts(70), world["drill"]))
    make_event(db, world["drill"], occurred_at=ts(70), actor_desc="crew member in blue shirt",
               from_location="LAB1S2", to_location="NOD2D4")
    dreamer.run(hours=6, now=ts(80))
    rows = memory_rows(db)
    assert "NOD2D4" in rows[("entity", world["drill"])]["summary"]
    assert len([k for k in rows if k == ("entity", world["drill"])]) == 1
    assert len(rows) >= n


def test_ac_agt_06_writes_only_agent_memory(dreamer, world, db):
    before = table_counts(db)
    dreamer.run(hours=6, now=NOW)
    after = table_counts(db)
    assert after["agent_memory"] > 0
    assert {k: v for k, v in after.items() if k != "agent_memory"} == \
        {k: v for k, v in before.items() if k != "agent_memory"}


def test_ac_agt_06_empty_window_still_writes_global(dreamer, db):
    dreamer.run(hours=6, now=NOW)
    assert ("global", "global") in memory_rows(db)


@pytest.mark.parametrize("hours", [0, -1])
def test_ac_agt_06_rejects_non_positive_hours(dreamer, db, hours):
    with pytest.raises(ValueError):
        dreamer.run(hours=hours, now=NOW)


# ---------------------------------------------------------------- periodic job + CLI
async def test_ac_agt_06_periodic_job_runs_repeatedly_until_stopped():
    stop = asyncio.Event()
    calls = []

    class Counting:
        def run(self, hours=6.0, now=None):
            calls.append(hours)
            if len(calls) >= 3:
                stop.set()
            return {"entities": 0, "locations": 0, "global": 1}

    await asyncio.wait_for(dream_forever(Counting(), interval_s=0.01, hours=2.5, stop=stop), timeout=5)
    assert len(calls) >= 3 and set(calls) == {2.5}


async def test_ac_agt_06_periodic_job_survives_a_failed_run():
    stop = asyncio.Event()
    calls = []

    class Flaky:
        def run(self, hours=6.0, now=None):
            calls.append(1)
            if len(calls) == 1:
                raise RuntimeError("db blip")
            stop.set()
            return {"entities": 0, "locations": 0, "global": 1}

    await asyncio.wait_for(dream_forever(Flaky(), interval_s=0.01, hours=1, stop=stop), timeout=5)
    assert len(calls) >= 2


def test_ac_agt_06_cli_once(database_url, world, db, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", database_url)
    assert main(["--once", "--hours", "100000"]) == 0
    assert ("global", "global") in memory_rows(db)


# ---------------------------------------------------------------- POST /memory/dream
@pytest.fixture()
def app_client(settings, inventory, database_url, db):
    """Factory so a missing implementation fails the test rather than erroring the fixture."""
    from nasa_agent.main import create_app

    opened = []

    def _make():
        app = create_app(settings, llm=FakeLLM([]), inventory=inventory, memory=FakeMemory(),
                         inventory_block=FakeInventoryBlock(), dreamer=Dreamer(database_url))
        c = TestClient(app, raise_server_exceptions=False)
        c.__enter__()
        opened.append(c)
        return c

    yield _make
    for c in opened:
        c.__exit__(None, None, None)


def test_ac_agt_06_dream_endpoint(app_client, world, db):
    r = app_client().post("/memory/dream", json={"hours": 100000})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["global"] == 1 and body["entities"] >= 2 and body["locations"] >= 2
    assert ("entity", world["drill"]) in memory_rows(db)


def test_ac_agt_06_dream_endpoint_default_hours(app_client, db):
    r = app_client().post("/memory/dream")
    assert r.status_code == 200, r.text
    assert ("global", "global") in memory_rows(db)


@pytest.mark.parametrize("payload", [{"hours": 0}, {"hours": -3}, {"hours": "lots"}])
def test_ac_agt_06_dream_endpoint_validates_hours(app_client, db, payload):
    assert app_client().post("/memory/dream", json=payload).status_code == 422
