"""AC-AGT-14 (UI review UI-02): "Who used the drill?" took 57 s and never named anyone. Live there were 13 "drill"
entities; who_moved resolved only the 5 most recently seen, and the drill with a `used` event by "in blue shirt,
headset" was 9th. list_unknowns(location="Node 1") answered "no record" because "Node 1" is a display name, not NOD1."""
from __future__ import annotations

import json

from tests.fakes import E_UNK_NOD2, _item


def _drills(fake_inventory, n=13):
    ids = []
    for k in range(n):
        eid = f"{k:08d}-0000-4000-8000-0000000000d1"
        ids.append(eid)
        fake_inventory.items.append(_item(eid, "drill", status="provisional", last_location="NOD1",
                                          last_seen_at=f"2026-10-01T13:{59 - k:02d}:00Z"))
    return ids


async def test_ac_agt_14_who_moved_finds_actor_beyond_five_matches(toolbox, fake_inventory):
    ids = _drills(fake_inventory)
    ninth = ids[8]
    fake_inventory.events.append({"event_id": "ev-used-9", "event_type": "used", "entity_id": ninth,
                                  "actor_desc": "in blue shirt, headset", "from_location": None,
                                  "to_location": None, "occurred_at": "2026-10-01T13:30:00Z", "confidence": 0.7,
                                  "source": "pipeline"})
    r = await toolbox.call("who_moved", {"name": "drill", "event_types": ["used"]})
    assert r.ok and r.has_data
    assert "in blue shirt, headset" in json.dumps(r.data)
    assert any(c["entity_id"] == ninth for c in r.citations)


async def test_ac_agt_14_who_moved_resolves_at_most_twenty(toolbox, fake_inventory):
    _drills(fake_inventory, 30)
    await toolbox.call("who_moved", {"name": "drill"})
    assert len(fake_inventory.calls("/events")) <= 20
    item_params = fake_inventory.params(fake_inventory.calls("/items")[-1])
    assert item_params["limit"] == ["20"]


async def test_ac_agt_14_who_moved_sends_types_list_and_degrades(toolbox, fake_inventory):
    await toolbox.call("who_moved", {"name": "Nikon camera", "event_types": ["moved", "used"]})
    p = fake_inventory.params(fake_inventory.calls("/events")[-1])
    assert set(p["types"][0].split(",")) == {"moved", "used"}
    fake_inventory.reject_types = True
    fake_inventory.requests.clear()
    r = await toolbox.call("who_moved", {"name": "Nikon camera", "event_types": ["moved", "used"]})
    assert r.ok and "crew member, red shirt" in json.dumps(r.data)


async def test_ac_agt_14_who_moved_keeps_newest_twenty_events_overall(toolbox, fake_inventory):
    ids = _drills(fake_inventory, 5)
    for k, eid in enumerate(ids):
        for j in range(8):
            fake_inventory.events.append({"event_id": f"ev-{k}-{j}", "event_type": "used", "entity_id": eid,
                                          "actor_desc": f"crew {k}-{j}", "from_location": None, "to_location": None,
                                          "occurred_at": f"2026-10-01T1{k}:{10 + j:02d}:00Z", "confidence": 0.7,
                                          "source": "pipeline"})
    r = await toolbox.call("who_moved", {"name": "drill", "event_types": ["used"]})
    events = [m for entry in r.data for m in entry["moves"]]
    assert len(events) == 20
    newest = sorted((m["occurred_at"] for m in events), reverse=True)
    assert newest[0].startswith("2026-10-01T14:17")
    assert [e["moves"][0]["occurred_at"] for e in r.data] == sorted(
        (e["moves"][0]["occurred_at"] for e in r.data), reverse=True), "entities ordered by newest event"


async def test_ac_agt_14_list_unknowns_accepts_module_display_name(toolbox, fake_inventory):
    r = await toolbox.call("list_unknowns", {"location": "Node 2"})
    assert r.ok and r.has_data
    assert {i["entity_id"] for i in r.data} == {E_UNK_NOD2}
    assert fake_inventory.params(fake_inventory.calls("/items")[-1])["location"] == ["NOD2"]


async def test_ac_agt_14_list_unknowns_location_id_still_works(toolbox, fake_inventory):
    r = await toolbox.call("list_unknowns", {"location": "nod2"})
    assert r.ok and {i["entity_id"] for i in r.data} == {E_UNK_NOD2}
