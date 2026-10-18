"""Live fixtures captured read-only from the deployed API on 2026-10-18 ~01:30 UTC (UI review round 2).

GET /api/items?q=drill (13 drills + 1 camera), GET /api/events?entity_id=a454e74e... (the one `used` event),
where_is("laptop") (5 laptops), recent_changes(since 00:33:43Z) and the two drafts the deployed model (agent-llm)
wrote for "Who used the drill?" - both were rejected by the claim check and replaced with "no record"."""
from __future__ import annotations

from datetime import UTC, datetime

from tests.fakes import _item

NOW = datetime(2026, 10, 18, 1, 20, 0, tzinfo=UTC)
DRILL_USED = "a454e74e-9a85-4677-9453-3574b69a7b93"

LIVE_DRILLS = [
    ("f4bb626b-ca22-4a8d-abaf-44da2c99eb97", "NOD1", "2026-10-17T22:43:50.178660Z"),
    ("9aa9f4df-8bfe-4c2f-8b62-99435e381342", "USOS", "2026-10-17T22:43:25.178660Z"),
    ("bacd6b54-bf6f-4f0e-9cd7-8556782c9683", "ZARYA", "2026-10-17T23:01:25.178660Z"),
    ("29dd3373-e366-41fc-9136-034151ff150e", "NOD1", "2026-10-17T22:58:48.178660Z"),
    ("60ec1c55-0f9b-477e-a105-92bc65bd25be", "NOD1", "2026-10-17T22:58:24.178660Z"),
    ("f3896cb5-20d0-4ef7-9c9c-4ce78e0badd3", "NOD1", "2026-10-17T22:58:24.178660Z"),
    ("d3a34c3a-2596-4513-a400-1d81c74e6387", "LAB1", "2026-10-17T22:58:00.178660Z"),
    ("22110eb7-eec6-404a-938a-a7a11eb0c0d8", "NOD2", "2026-10-17T22:57:36.178660Z"),
    (DRILL_USED, "USOS", "2026-10-17T22:57:00.178660Z"),
    ("09d2a49c-127f-443d-a3b7-055558af9ffd", "USOS", "2026-10-17T22:49:36.178660Z"),
    ("ed6174db-0e78-461c-8742-380c67985049", "ZVEZDA", "2026-10-17T22:47:48.178660Z"),
    ("50d19a6a-64d8-4432-8505-16c9fea4dedd", "ZARYA", "2026-10-17T22:46:36.178660Z"),
    ("cb35e217-5ac0-44d5-99c1-e35477a3102f", "NOD2", "2026-10-17T22:45:36.178660Z"),
]

#: GET /api/events?entity_id=a454e74e-... verbatim.
LIVE_DRILL_EVENT = {
    "event_id": "fd45d05c-b095-4eb2-92ea-ba105303ec23",
    "event_type": "used",
    "entity_id": DRILL_USED,
    "actor_entity": None,
    "actor_desc": "in blue shirt, headset",
    "from_location": "USOS",
    "to_location": None,
    "occurred_at": "2026-10-17T22:53:24.178660Z",
    "confidence": 0.8,
    "evidence_url": "/media/frames/cam-handheld/2026-10-17/225324.178660.jpg",
    "source": "pipeline",
    "created_at": "2026-10-17T23:18:12.704190Z",
    "entity_label": "drill",
    "entity_catalog_id": "drill-pistol",
    "from_location_name": "US Orbital Segment",
    "to_location_name": None,
}
LIVE_ADDED_EVENT = {
    "event_id": "0a0a0a0a-0000-4000-8000-0000000000ad", "event_type": "added",
    "entity_id": "09d2a49c-127f-443d-a3b7-055558af9ffd", "actor_entity": None, "actor_desc": None,
    "from_location": None, "to_location": "USOS", "occurred_at": "2026-10-17T22:45:53.178660Z", "confidence": 1.0,
    "evidence_url": None, "source": "operator",
}

#: The deployed model's drafts for "Who used the drill?" (agent-llm, captured in the agent pod).
LIVE_DRILL_DRAFT_1 = ("The drill was used by a crew member in a blue shirt with a headset at USOS. This occurred at "
                      "22:53:24 UTC on October 17, 2026.")
LIVE_DRILL_DRAFT_2 = ('The drill was used by a crew member described as "in blue shirt, headset" at USOS. The event '
                      "occurred on 2026-10-17 at 22:53:24 UTC.")

LIVE_LAPTOPS = [
    ("da9709e7-4ed6-4b2d-beb0-aa6c814ae764", "USOS", "US Orbital Segment", "2026-10-17T22:51:03.178660Z"),
    ("5a93122e-5dd1-434a-8910-6b318890faa8", "NOD1", "Node 1 Unity", "2026-10-17T22:43:36.178660Z"),
    ("163e1c50-a833-49c1-a2bb-3f3100d38cc0", "NOD1", "Node 1 Unity", "2026-10-17T23:00:27.178660Z"),
    ("b24b8ca5-1a8d-4d63-bef8-814b03080710", "NOD3", "Node 3 Tranquility", "2026-10-17T23:00:12.178660Z"),
    ("9cd7ba16-8701-4d4f-b47b-c932a43908e9", "USOS", "US Orbital Segment", "2026-10-17T22:59:40.178660Z"),
]

#: The reviewer's cut-off laptop answer (UI2-03), shape of the live reply at the 400-token cap.
LIVE_LAPTOP_CUT = (
    "Here are the laptops:\n\n"
    "1. **Laptop**\n   - Location: USOS (US Orbital Segment)\n   - Last Seen: 2026-10-17T22:51:03Z\n\n"
    "1. **Laptop**\n   - Location: NOD1 (Node 1 Unity)\n   - Last Seen: 2026-10-17T23:00:27Z\n\n"
    "1. **Laptop**\n   - Location: NOD3 (Node 3 Tranquility)\n   - Last Seen: 2026-10-17T23:00:12Z **Conf"
)

EXTRA_LOCATIONS = [
    {"location_id": "ROS", "parent_id": None, "kind": "segment", "display_name": "Russian Orbital Segment"},
    {"location_id": "ZARYA", "parent_id": "ROS", "kind": "module", "display_name": "Zarya FGB"},
    {"location_id": "ZVEZDA", "parent_id": "ROS", "kind": "module", "display_name": "Zvezda Service Module"},
]


def load_live(fake_inventory, toolbox=None) -> None:
    """Replace the fake dataset's drills/laptops with the live shapes (and pin the toolbox clock to NOW)."""
    old = {i["entity_id"] for i in fake_inventory.items if "drill" in i["label"]}
    fake_inventory.items = [i for i in fake_inventory.items if i["entity_id"] not in old]
    for eid, loc, seen in LIVE_DRILLS:
        fake_inventory.items.append(_item(eid, "drill", catalog_id="drill-pistol", status="provisional",
                                          last_location=loc, last_seen_at=seen, confidence=0.5))
    for eid, loc, name, seen in LIVE_LAPTOPS:
        fake_inventory.items.append(_item(eid, "laptop", catalog_id="laptop-ssc", catalog_name="SSC laptop",
                                          status="provisional", last_location=loc, last_location_name=name,
                                          last_seen_at=seen, confidence=0.5))
    fake_inventory.events = [e for e in fake_inventory.events if e["entity_id"] not in old]
    fake_inventory.events += [dict(LIVE_DRILL_EVENT), dict(LIVE_ADDED_EVENT)]
    fake_inventory.locations += [dict(x) for x in EXTRA_LOCATIONS]
    if toolbox is not None:
        toolbox.now = lambda: NOW
