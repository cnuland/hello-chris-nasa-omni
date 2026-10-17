"""AC-AGT-2 amendment (cluster integration 2026-10-17): "Who was using the drill?" got "I don't have a record of
that" although the pipeline had recorded three `used` events by "Astronaut in blue space suit". who_moved only
asked /events for type=moved. It now covers every handling event type by default: moved, used, handed_over, added,
removed, stowed, unstowed and consumed (but not plain `seen` sightings). event_types narrows the set."""
from .fakes import E_DRILL


def _blob(data):
    import json

    return json.dumps(data)


async def test_who_moved_includes_used_and_handed_over_events(toolbox, fake_inventory):
    fake_inventory.events += [
        {"event_id": "e-used-1", "event_type": "used", "entity_id": E_DRILL, "actor_desc": "astronaut in blue suit",
         "from_location": None, "to_location": None, "occurred_at": "2026-10-01T13:00:00Z", "confidence": 0.8,
         "source": "pipeline"},
        {"event_id": "e-hand-1", "event_type": "handed_over", "entity_id": E_DRILL, "actor_desc": "crew member, cap",
         "from_location": None, "to_location": None, "occurred_at": "2026-10-01T13:05:00Z", "confidence": 0.7,
         "source": "pipeline"},
        {"event_id": "e-seen-1", "event_type": "seen", "entity_id": E_DRILL, "actor_desc": "should not appear",
         "from_location": None, "to_location": None, "occurred_at": "2026-10-01T13:10:00Z", "confidence": 0.9,
         "source": "pipeline"},
    ]
    r = await toolbox.call("who_moved", {"name": "drill"})
    assert r.ok and r.has_data
    b = _blob(r.data)
    assert "astronaut in blue suit" in b and "crew member, cap" in b
    assert "crew member in blue shirt" in b  # existing moved events still returned
    assert "should not appear" not in b  # plain sightings are not handling events
    assert '"used"' in b and '"handed_over"' in b  # each entry says what kind of handling it was


async def test_who_moved_event_types_narrows(toolbox, fake_inventory):
    fake_inventory.events.append(
        {"event_id": "e-used-2", "event_type": "used", "entity_id": E_DRILL, "actor_desc": "astronaut in blue suit",
         "from_location": None, "to_location": None, "occurred_at": "2026-10-01T13:00:00Z", "confidence": 0.8,
         "source": "pipeline"})
    r = await toolbox.call("who_moved", {"name": "drill", "event_types": ["used"]})
    b = _blob(r.data)
    assert "astronaut in blue suit" in b and "crew member in blue shirt" not in b


async def test_who_moved_rejects_unknown_event_type(toolbox, fake_inventory):
    r = await toolbox.call("who_moved", {"name": "drill", "event_types": ["teleported"]})
    assert not r.ok and r.error_kind == "validation"


def test_who_moved_description_mentions_using_and_handling(toolbox):
    spec = {t["function"]["name"]: t["function"] for t in toolbox.specs()}["who_moved"]
    d = spec["description"].lower()
    assert "used" in d and ("handed" in d or "handled" in d)
    assert "event_types" in spec["parameters"]["properties"]
