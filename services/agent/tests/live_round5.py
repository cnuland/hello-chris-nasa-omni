"""Round-5 fixtures (docs/ui-review/round5, 2026-10-18): live person tracks with their sightings, captured read-only
from GET /api/items?kind=person and GET /api/items/{id} at ~06:50 UTC, and a 59-change hour for paging.

At capture time no person track had CUP1 as its last location, but four tracks had a CUP1 sighting: f77dc775 and
28513d12 (06:46:38), d1c983d2 (06:31:38, the frame the reviewer placed in Node 1: a perception carry error) and
395bae46 (06:31:08, the captioned Cupola frame). UI5-04: "Who was in the Cupola?" filtered on last_location."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

from tests.fakes import _item
from tests.live_round4 import _event

NOW = datetime(2026, 10, 18, 6, 50, 0, tzinfo=UTC)

#: (entity_id, description, last_location, first_seen_at, last_seen_at, sightings newest first)
LIVE_PEOPLE_5 = [
    ('34eb4266-973c-42e1-aab8-c0e54d86c762', 'green shirt, dark shorts, glasses', 'ZVEZDA', '2026-10-18T06:49:38.555353Z', '2026-10-18T06:49:38.555353Z', [('ZVEZDA', '2026-10-18T06:49:38.555353Z')]),
    ('98e98364-4c52-4985-9189-aff3a6b06b5e', 'wearing white shirt', 'USOS', '2026-10-18T06:49:08.555353Z', '2026-10-18T06:49:08.555353Z', [('USOS', '2026-10-18T06:49:08.555353Z')]),
    ('f77dc775-9d98-40a8-9185-93e07841a91b', 'blue shirt, dark hair', 'USOS', '2026-10-18T06:28:38.555353Z', '2026-10-18T06:49:08.555353Z', [('USOS', '2026-10-18T06:49:08.555353Z'), ('USOS', '2026-10-18T06:47:08.555353Z'), ('CUP1', '2026-10-18T06:46:38.555353Z'), ('CUP1', '2026-10-18T06:46:08.555353Z'), ('NOD1', '2026-10-18T06:45:38.555353Z'), ('USOS', '2026-10-18T06:38:38.555353Z'), ('NAUKA', '2026-10-18T06:36:38.555353Z'), ('USOS', '2026-10-18T06:32:08.555353Z'), ('NOD1', '2026-10-18T06:30:38.555353Z'), ('JPM1', '2026-10-18T06:28:38.555353Z')]),
    ('28513d12-2811-4ef2-a9ac-bc4dea47cd7d', 'short hair, grey t-shirt, black shorts', 'USOS', '2026-10-18T06:30:38.555353Z', '2026-10-18T06:47:08.555353Z', [('USOS', '2026-10-18T06:47:08.555353Z'), ('CUP1', '2026-10-18T06:46:38.555353Z'), ('NOD1', '2026-10-18T06:45:38.555353Z'), ('USOS', '2026-10-18T06:32:08.555353Z'), ('CUP1', '2026-10-18T06:31:38.555353Z'), ('NOD1', '2026-10-18T06:30:38.555353Z')]),
    ('395bae46-07da-4289-9f11-8fc40b39cd7f', 'grey long-sleeve shirt, blue shorts', 'USOS', '2026-10-18T06:31:08.555353Z', '2026-10-18T06:47:08.555353Z', [('USOS', '2026-10-18T06:47:08.555353Z'), ('USOS', '2026-10-18T06:39:38.555353Z'), ('USOS', '2026-10-18T06:39:08.555353Z'), ('USOS', '2026-10-18T06:38:38.555353Z'), ('ZVEZDA', '2026-10-18T06:35:08.555353Z'), ('USOS', '2026-10-18T06:32:08.555353Z'), ('CUP1', '2026-10-18T06:31:08.555353Z')]),
    ('61a9d598-89d9-4750-b528-55521b7da975', 'black shirt, grey pants', 'USOS', '2026-10-18T06:28:08.555353Z', '2026-10-18T06:47:08.555353Z', [('USOS', '2026-10-18T06:47:08.555353Z'), ('COL1', '2026-10-18T06:28:08.555353Z')]),
    ('d1c983d2-f272-41e9-8837-d31229b15962', 'blue shirt, orange helmet', 'USOS', '2026-10-18T06:31:38.555353Z', '2026-10-18T06:43:38.555353Z', [('USOS', '2026-10-18T06:43:38.555353Z'), ('USOS', '2026-10-18T06:39:08.555353Z'), ('CUP1', '2026-10-18T06:31:38.555353Z')]),
    ('1ea5e31f-2d43-450c-8012-7ed9f242f756', 'blue flight suit with American flag patch', 'USOS', '2026-10-18T06:27:38.555353Z', '2026-10-18T06:42:38.555353Z', [('USOS', '2026-10-18T06:42:38.555353Z'), ('USOS', '2026-10-18T06:27:38.555353Z')]),
    ('32652395-9f9c-4a8e-a481-0988ec2bdfa5', 'black shirt, blue pants', 'USOS', '2026-10-18T06:32:38.555353Z', '2026-10-18T06:41:38.555353Z', [('USOS', '2026-10-18T06:41:38.555353Z'), ('USOS', '2026-10-18T06:32:38.555353Z')]),
    ('8bf24e89-2a91-4449-8307-2b0f1399b037', 'white suit, blue gloves', 'USOS', '2026-10-18T06:34:08.555353Z', '2026-10-18T06:41:08.555353Z', [('USOS', '2026-10-18T06:41:08.555353Z'), ('USOS', '2026-10-18T06:34:08.555353Z')]),
    ('bd86ab50-a24b-4f9a-b3b7-61812f60e5b2', 'grey hoodie', 'USOS', '2026-10-18T06:39:38.555353Z', '2026-10-18T06:39:38.555353Z', [('USOS', '2026-10-18T06:39:38.555353Z')]),
    ('1bd00ece-1c48-4b36-a986-d29dd4982808', 'white spacesuit, helmet', 'USOS', '2026-10-18T06:39:08.555353Z', '2026-10-18T06:39:08.555353Z', [('USOS', '2026-10-18T06:39:08.555353Z')]),
    ('b1633268-4086-4d21-981a-26124d42f484', 'blue suit, orange gloves', 'NAUKA', '2026-10-18T06:36:08.555353Z', '2026-10-18T06:36:08.555353Z', [('NAUKA', '2026-10-18T06:36:08.555353Z')]),
    ('8c58d341-b1c3-4e08-9452-3749e0df4976', 'blue shirt, shorts, glasses', 'ZVEZDA', '2026-10-18T06:34:38.555353Z', '2026-10-18T06:34:38.555353Z', [('ZVEZDA', '2026-10-18T06:34:38.555353Z')]),
]
CUPOLA_VISITORS = [  # newest CUP1 sighting first (ties: higher entity id first)
    "f77dc775-9d98-40a8-9185-93e07841a91b", "28513d12-2811-4ef2-a9ac-bc4dea47cd7d",
    "d1c983d2-f272-41e9-8837-d31229b15962", "395bae46-07da-4289-9f11-8fc40b39cd7f",
]
#: GET /api/locations rows used here (the private Node 2 crew quarters and the Node 3 WHC are no_capture).
LOCATIONS_5 = [
    {"location_id": "ROS", "parent_id": None, "kind": "segment", "display_name": "Russian Orbital Segment"},
    {"location_id": "CUP1", "parent_id": "NOD3", "kind": "module", "display_name": "Cupola"},
    {"location_id": "COL1", "parent_id": "USOS", "kind": "module", "display_name": "Columbus"},
    {"location_id": "JPM1", "parent_id": "USOS", "kind": "module", "display_name": "Kibo JEM Pressurized Module"},
    {"location_id": "ZVEZDA", "parent_id": "ROS", "kind": "module", "display_name": "Zvezda Service Module"},
    {"location_id": "NAUKA", "parent_id": "ROS", "kind": "module", "display_name": "Nauka MLM"},
    {"location_id": "NOD2D5", "parent_id": "NOD2", "kind": "zone", "display_name": "Node 2 crew quarters (deck)",
     "no_capture": True},
    {"location_id": "NOD2O5", "parent_id": "NOD2", "kind": "zone", "display_name": "Node 2 crew quarters (ovhd)",
     "no_capture": True},
    {"location_id": "NOD2P5", "parent_id": "NOD2", "kind": "zone", "display_name": "Node 2 crew quarters (port)",
     "no_capture": True},
    {"location_id": "NOD2S5", "parent_id": "NOD2", "kind": "zone", "display_name": "Node 2 crew quarters (stbd)",
     "no_capture": True},
    {"location_id": "NOD3F4", "parent_id": "NOD3", "kind": "zone", "display_name": "Node 3 WHC (toilet)",
     "no_capture": True},
]
#: 16 open unknown objects all over the station (the list_unknowns result the crew-quarters answer cited).
UNKNOWNS_5 = [(f"5f5f5f5f-0000-4000-8000-{n:012d}", label, loc) for n, (label, loc) in enumerate([
    ("yellow latch", "NOD1"), ("silver foil insulation", "USOS"), ("silver cylinder", "ZVEZDA"),
    ("white cloth cover", "LAB1"), ("grey object", "NOD1"), ("treadmill", "ZVEZDA"), ("black strap", "USOS"),
    ("gas cylinder", "JPM1"), ("blue container", "COL1"), ("orange bag", "NOD3"), ("white box", "USOS"),
    ("cable bundle", "LAB1"), ("green pouch", "NAUKA"), ("red handle", "NOD2"), ("metal bracket", "USOS"),
    ("tan bag", "CUP1"),
])]


def load_round5(fake_inventory, toolbox=None) -> None:
    """Live round-5 people with sightings, the round-5 locations (private zones flagged) and 16 unknowns; no drill.
    Pins the toolbox clock to NOW."""
    fake_inventory.items = [i for i in fake_inventory.items if i["kind"] != "person"
                            and i["catalog_id"] != "drill-pistol"]
    fake_inventory.events = [e for e in fake_inventory.events if e["entity_id"] in {i["entity_id"]
                                                                                    for i in fake_inventory.items}]
    for eid, desc, loc, first, last, sightings in LIVE_PEOPLE_5:
        item = _item(eid, "person", kind="person", catalog_id="crew-member", catalog_name="Crew member",
                     status="provisional", last_location=loc, last_seen_at=last, confidence=0.5, description=desc)
        item["first_seen_at"] = first
        fake_inventory.items.append(item)
        fake_inventory.sightings[eid] = [{"location_id": lid, "seen_at": at, "bbox": None, "is_private": False}
                                         for lid, at in sightings]
    for eid, label, loc in UNKNOWNS_5:
        unk = _item(eid, label, status="unknown", last_location=loc, last_seen_at="2026-10-18T06:40:00Z",
                    confidence=0.3, needs_review=True)
        unk["first_seen_at"] = "2026-10-18T06:00:00Z"
        fake_inventory.items.append(unk)
    known = {loc["location_id"] for loc in fake_inventory.locations}
    fake_inventory.locations += [dict(x) for x in LOCATIONS_5 if x["location_id"] not in known]
    if toolbox is not None:
        toolbox.now = lambda: NOW
        toolbox._locations = None


def add_hour_of_changes(fake_inventory, n: int = 59) -> list[str]:
    """``n`` laptop moves in the hour before NOW (one a minute, from 06:49:30 back), newest first event ids. Every
    item is made first-seen before the hour, so the hour's changes are exactly these moves."""
    for i in fake_inventory.items:
        i["first_seen_at"] = "2026-10-18T05:00:00Z"
    ids = []
    for k in range(n):
        at = NOW - timedelta(seconds=30 + 60 * k)
        eid = f"7c7c7c7c-0000-4000-8000-{k:012d}"
        fake_inventory.events.append(_event(eid, "moved", "3735b3dc-0d6a-4c1e-9a43-0f7e1a3b5c01",
                                            at.strftime("%Y-%m-%dT%H:%M:%SZ"), label=f"item {k + 1:02d}",
                                            frm="USOS", to="NOD1", catalog_id="laptop"))
        ids.append(eid)
    return ids
