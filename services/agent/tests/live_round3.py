"""Live fixtures captured read-only from the deployed API on 2026-10-18 ~03:30 UTC (UI review round 3).

GET /api/items?kind=person (17 person tracks: USOS 10, NOD1 2, NOD3 2, CUP1 1, ZVEZDA 1, COL1 1, the reviewer's
numbers), one person-track `moved` event with a different actor description (UI3-04), the operator move of the cargo
transfer bag by ui-review-3, the module list, and the deployed model's wrong draft for the people question (UI3-01)."""
from __future__ import annotations

from datetime import UTC, datetime

from tests.fakes import _item

NOW = datetime(2026, 10, 18, 3, 32, 0, tzinfo=UTC)

#: (entity_id, description, last_location, first_seen_at, last_seen_at)
LIVE_PEOPLE = [
    ('cef63c49-f74f-4c1e-881e-16d2c5880c02', 'black shirt, blue pants', 'USOS', '2026-10-18T03:07:48.878740Z', '2026-10-18T03:31:48.878740Z'),
    ('a64f199d-01fd-4662-8b14-f3c654f40b5f', 'grey shirt, black shorts', 'USOS', '2026-10-18T03:06:48.878740Z', '2026-10-18T03:29:48.878740Z'),
    ('0d53940f-91b0-44f6-b560-ced60a4f2490', 'blue shirt, dark hair', 'USOS', '2026-10-18T03:03:48.878740Z', '2026-10-18T03:28:48.878740Z'),
    ('8af9093f-c05f-4453-b776-62b61bca4955', 'dark hair, purple jacket', 'USOS', '2026-10-18T03:22:18.878740Z', '2026-10-18T03:24:13.878740Z'),
    ('7e7494d6-bc8c-4a57-8f41-0896362117cd', 'blue shorts, grey long-sleeve shirt', 'CUP1', '2026-10-18T03:06:18.878740Z', '2026-10-18T03:21:18.878740Z'),
    ('234abef7-cc4e-4649-9593-0804d8bc504c', 'dark hair, dark blue shirt', 'NOD1', '2026-10-18T03:05:48.878740Z', '2026-10-18T03:20:48.878740Z'),
    ('c3f8e2e9-c1ca-47c9-93ff-9620b84b97a0', 'short hair, grey t-shirt, black shorts', 'NOD1', '2026-10-18T03:05:48.878740Z', '2026-10-18T03:20:48.878740Z'),
    ('f2944310-a542-4692-89ef-e0dd62baa077', 'blue flight suit with American flag patch', 'USOS', '2026-10-18T03:02:48.878740Z', '2026-10-18T03:17:48.878740Z'),
    ('216784ae-3b4d-4e38-82e8-bc092eb98288', 'white suit, reaching forward', 'USOS', '2026-10-18T03:16:18.878740Z', '2026-10-18T03:16:18.878740Z'),
    ('404ba16c-a4e7-4d2b-9bb9-3f6a8cc8822e', 'grey hoodie', 'USOS', '2026-10-18T03:14:48.878740Z', '2026-10-18T03:14:48.878740Z'),
    ('87dbd193-539f-4fec-82a5-29210c7a4071', 'Short hair, blue shorts, orange top', 'USOS', '2026-10-18T03:10:18.878740Z', '2026-10-18T03:14:18.878740Z'),
    ('9f6a9b48-7d09-4097-be6b-f38e3b732f7b', 'dark hair, blue tank top, dark shorts', 'NOD3', '2026-10-18T03:13:48.878740Z', '2026-10-18T03:13:48.878740Z'),
    ('a2a79892-fc14-417a-8f10-4a22d1770078', 'grey hair, grey long-sleeve shirt, dark pants', 'NOD3', '2026-10-18T03:13:48.878740Z', '2026-10-18T03:13:48.878740Z'),
    ('90bbed98-b768-4949-a063-4b439486aa8a', 'wearing a blue shirt and shorts', 'ZVEZDA', '2026-10-18T03:09:48.878740Z', '2026-10-18T03:09:48.878740Z'),
    ('7d53e82d-bbbc-4ca5-9caa-c9de48f435fa', 'white shirt, blue pants', 'USOS', '2026-10-18T03:09:18.878740Z', '2026-10-18T03:09:18.878740Z'),
    ('f51af769-c84f-4111-a50d-6e06b3a4bd61', 'grey shirt, dark hair', 'USOS', '2026-10-18T03:07:18.878740Z', '2026-10-18T03:07:18.878740Z'),
    ('1b4b2e61-06f6-40fb-8504-62d09f694b62', 'black shirt, grey pants', 'COL1', '2026-10-18T03:03:18.878740Z', '2026-10-18T03:03:18.878740Z'),
]
EXPECTED_BY_MODULE = {"USOS": 10, "NOD1": 2, "NOD3": 2, "CUP1": 1, "ZVEZDA": 1, "COL1": 1}
PERSON_87 = "87dbd193-539f-4fec-82a5-29210c7a4071"
PERSON_CUP = "7e7494d6-bc8c-4a57-8f41-0896362117cd"
CTB = "2348f197-823a-4630-9111-1edf50cd6015"

#: GET /api/events?entity_id=87dbd193-... verbatim: a person track "moved" with someone else's actor text.
LIVE_PERSON_MOVE = {
    "event_id": "675317fa-3747-404d-9ee8-bf8c08bca271", "event_type": "moved", "entity_id": PERSON_87,
    "actor_entity": None, "actor_desc": "spacesuit, dark visor", "from_location": "USOS", "to_location": None,
    "occurred_at": "2026-10-18T03:10:48.878740Z", "confidence": 0.9,
    "evidence_url": "/media/frames/cam-handheld/2026-10-18/031048.878740.jpg", "source": "pipeline",
    "created_at": "2026-10-18T03:11:07.210777Z", "location_role": "from_to", "actor_kind": "person",
    "entity_label": "person", "entity_catalog_id": "crew-member", "from_location_name": "US Orbital Segment",
    "to_location_name": None,
}
#: A person-track arrival (same shape, to_location only).
LIVE_PERSON_ARRIVAL = {
    **LIVE_PERSON_MOVE, "event_id": "675317fa-3747-404d-9ee8-bf8c08bca272", "entity_id": PERSON_CUP,
    "actor_desc": "grey t-shirt, dark shorts", "from_location": None, "to_location": "CUP1",
    "occurred_at": "2026-10-18T03:21:18.878740Z", "from_location_name": None, "to_location_name": "Cupola",
}
#: GET /api/events?source=operator verbatim (the reviewer's location edit).
LIVE_OPERATOR_MOVE = {
    "event_id": "487ca3ab-c98f-40a9-a379-c6a51124980b", "event_type": "moved", "entity_id": CTB,
    "actor_entity": None, "actor_desc": "ui-review-3", "from_location": "USOS", "to_location": "JPM1",
    "occurred_at": "2026-10-18T03:22:52.073082Z", "confidence": 1.0, "evidence_url": None, "source": "operator",
    "created_at": "2026-10-18T03:22:52.073082Z", "location_role": "from_to", "actor_kind": "operator",
    "entity_label": "cargo transfer bag", "entity_catalog_id": "ctb-half", "from_location_name": "US Orbital Segment",
    "to_location_name": "Kibo JEM Pressurized Module",
}

LIVE_MODULES = [
    {"location_id": "ROS", "parent_id": None, "kind": "segment", "display_name": "Russian Orbital Segment"},
    {"location_id": "ZVEZDA", "parent_id": "ROS", "kind": "module", "display_name": "Zvezda Service Module"},
    {"location_id": "CUP1", "parent_id": "USOS", "kind": "module", "display_name": "Cupola"},
    {"location_id": "COL1", "parent_id": "USOS", "kind": "module", "display_name": "Columbus"},
    {"location_id": "JPM1", "parent_id": "USOS", "kind": "module", "display_name": "Kibo JEM Pressurized Module"},
]

#: The deployed model's answer to "How many people have been seen today, and where?" (UI3-01, chat-q7).
LIVE_PEOPLE_DRAFT = ("Today, 4 people have been recorded as new items:\n1. person: USOS, 03:14 UTC\n"
                     "2. person: USOS, 03:10 UTC\n3. person: NOD3, 03:13 UTC\n4. person: NOD3, 03:13 UTC")


def load_people(fake_inventory, toolbox=None) -> None:
    """Add the 17 live person tracks, the live modules and the three live events; pin the toolbox clock to NOW."""
    fake_inventory.items = [i for i in fake_inventory.items if i["kind"] != "person"]
    for eid, desc, loc, first, last in LIVE_PEOPLE:
        item = _item(eid, "person", kind="person", catalog_id="crew-member", catalog_name="Crew member",
                     status="provisional", last_location=loc, last_seen_at=last, confidence=0.5, description=desc)
        item["first_seen_at"] = first
        fake_inventory.items.append(item)
    ctb = _item(CTB, "cargo transfer bag", catalog_id="ctb-half", status="provisional", last_location="JPM1",
                last_seen_at="2026-10-18T03:22:52.073082Z")
    ctb["first_seen_at"] = "2026-10-18T01:00:00Z"
    fake_inventory.items.append(ctb)
    known = {loc["location_id"] for loc in fake_inventory.locations}
    fake_inventory.locations += [dict(x) for x in LIVE_MODULES if x["location_id"] not in known]
    fake_inventory.events = [dict(LIVE_PERSON_MOVE), dict(LIVE_PERSON_ARRIVAL), dict(LIVE_OPERATOR_MOVE)]
    fake_inventory.reviews = []
    if toolbox is not None:
        toolbox.now = lambda: NOW
