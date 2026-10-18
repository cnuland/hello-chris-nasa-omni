"""Round-4 fixtures (docs/ui-review/round4, 2026-10-18): the standard conversation and a live-shaped dataset.

Built on the round-3 live people (tests/live_round3.py, clock pinned to 03:32 UTC) plus what the round-4 reviewer saw:
two Cupola person tracks, 9 laptop items (one flagged as a possible duplicate, with an open possible_duplicate review),
events without any location ("Camera moved", "Laptop used by person (...)", shapes from GET /api/events), two drill
events in the same hour (the drill was cited twice), and unknown objects in Node 1. The five earlier turns of the
standard conversation are kept as the dashboard sends them (user + assistant)."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

from tests.fakes import E_CAMERA, E_DRILL, _item
from tests.live_round3 import load_people

CUP_2 = "299c70cd-2e12-4038-b01b-6d8e06aba197"
CUP_1 = "7e7494d6-bc8c-4a57-8f41-0896362117cd"
#: (entity_id, last_location, last_seen_at) of the 9 live laptop items (GET /api/items?q=laptop, total 9)
LAPTOPS = [
    ("3735b3dc-0d6a-4c1e-9a43-0f7e1a3b5c01", "USOS", "2026-10-18T03:30:49Z"),
    ("1c9371ba-7d2b-4c55-8a11-2b4f6c8d9e02", "NAUKA", "2026-10-18T03:28:19Z"),
    ("2f9501cb-2a3c-4d66-9b22-3c5a7d9e0f03", "ZVEZDA", "2026-10-18T03:26:19Z"),
    ("d977201c-327c-49eb-8c44-147a1d9cf4b2", "CUP1", "2026-10-18T03:21:49Z"),
    ("25235c4b-4c5e-4f88-8d44-5e7c9f1a2b05", "USOS", "2026-10-18T03:15:19Z"),
    ("6b3eb27f-5d6f-4a99-9e55-6f8d0a2b3c06", "NOD3", "2026-10-18T03:13:49Z"),
    ("6987ebac-6e7a-4baa-8f66-7a9e1b3c4d07", "JPM1", "2026-10-18T03:03:53Z"),
    ("29bd82bf-7f8b-4cbb-9a77-8b0f2c4d5e08", "ZVEZDA", "2026-10-18T02:54:49Z"),
    ("f35c003c-8a9c-4dcc-8b88-9c1a3d5e6f09", "NOD2", "2026-10-18T02:49:52Z"),
]
LAPTOP_DUP = LAPTOPS[1][0]
MULTIMETER = "4d4d4d4d-1111-4222-8333-444444444444"
UNK_NOD1 = ["5e5e5e5e-1111-4222-8333-444444444441", "5e5e5e5e-1111-4222-8333-444444444442"]
EV_DRILL_USED = "7a7a7a7a-0000-4000-8000-000000000001"
EV_DRILL_MOVED = "7a7a7a7a-0000-4000-8000-000000000002"
EV_CAMERA_NOWHERE = "7a7a7a7a-0000-4000-8000-000000000003"
EV_LAPTOP_USED = "7a7a7a7a-0000-4000-8000-000000000004"

NAUKA = {"location_id": "NAUKA", "parent_id": "ROS", "kind": "module", "display_name": "Nauka MLM"}
JLP1 = {"location_id": "JLP1", "parent_id": "USOS", "kind": "module", "display_name": "Kibo JEM Logistics Module"}
JPM1_ZONE = {"location_id": "JPM1F1", "parent_id": "JPM1", "kind": "zone", "display_name": "Kibo forward rack 1"}


def _event(event_id, event_type, entity_id, at, *, label, actor=None, frm=None, to=None, kind="object",
           catalog_id=None, actor_kind=None):
    return {
        "event_id": event_id, "event_type": event_type, "entity_id": entity_id, "actor_entity": None,
        "actor_desc": actor, "from_location": frm, "to_location": to, "occurred_at": at, "confidence": 0.9,
        "evidence_url": None, "source": "pipeline", "location_role": None if not (frm or to) else "from_to",
        "actor_kind": actor_kind, "entity_label": label, "entity_catalog_id": catalog_id, "entity_kind": kind,
        "from_location_name": None, "to_location_name": None,
    }


def load_round4(fake_inventory, toolbox=None) -> None:
    load_people(fake_inventory, toolbox)
    person = _item(CUP_2, "person", kind="person", catalog_id="crew-member", catalog_name="Crew member",
                   status="provisional", last_location="CUP1", last_seen_at="2026-10-18T03:30:54Z", confidence=0.5,
                   description="grey shirt, black shorts")
    person["first_seen_at"] = "2026-10-18T00:20:54Z"
    fake_inventory.items.append(person)
    for i in fake_inventory.items:
        if i["kind"] == "person" and i["entity_id"] != CUP_2:
            i["first_seen_at"] = "2026-10-18T00:30:00Z"  # people are not "new" in the last hour
    for eid, loc, seen in LAPTOPS:
        item = _item(eid, "laptop", catalog_id="laptop", catalog_name="SSC laptop", status="provisional",
                     last_location=loc, last_seen_at=seen, confidence=0.5, needs_review=eid == LAPTOP_DUP)
        item["review_reason"] = "possible_duplicate" if eid == LAPTOP_DUP else None
        item["first_seen_at"] = "2026-10-18T01:00:00Z"
        fake_inventory.items.append(item)
    meter = _item(MULTIMETER, "multimeter", catalog_id="multimeter", catalog_name="Fluke multimeter",
                  status="provisional", last_location="LAB1", last_seen_at="2026-10-18T03:05:00Z")
    meter["first_seen_at"] = "2026-10-18T01:00:00Z"
    fake_inventory.items.append(meter)
    for eid, label in zip(UNK_NOD1, ("unknown grey strap", "unknown white bag"), strict=True):
        unk = _item(eid, label, status="unknown", last_location="NOD1", last_seen_at="2026-10-18T03:00:00Z",
                    confidence=0.3, needs_review=True)
        unk["first_seen_at"] = "2026-10-18T01:00:00Z"
        fake_inventory.items.append(unk)
    known = {loc["location_id"] for loc in fake_inventory.locations}
    fake_inventory.locations += [dict(x) for x in (NAUKA, JLP1, JPM1_ZONE) if x["location_id"] not in known]
    fake_inventory.events += [
        _event(EV_DRILL_USED, "used", E_DRILL, "2026-10-18T03:10:00Z", label="pistol-grip drill",
               actor="blue shirt, dark shorts", frm="LAB1", catalog_id="drill-pistol", actor_kind="person"),
        _event(EV_DRILL_MOVED, "moved", E_DRILL, "2026-10-18T03:12:00Z", label="pistol-grip drill",
               actor="blue shirt, dark shorts", frm="LAB1", to="LAB1S2", catalog_id="drill-pistol",
               actor_kind="person"),
        _event(EV_CAMERA_NOWHERE, "moved", E_CAMERA, "2026-10-18T03:25:00Z", label="Nikon camera",
               catalog_id="camera-dslr"),
        _event(EV_LAPTOP_USED, "used", LAPTOPS[3][0], "2026-10-18T03:28:00Z", label="laptop",
               actor="grey t-shirt, black shorts", catalog_id="laptop", actor_kind="person"),
    ]
    # 23 more laptop moves in the hour (30 changes in all), so "what changed" needs paging as the live 76 did
    for n in range(23):
        eid = LAPTOPS[n % len(LAPTOPS)][0]
        at = datetime(2026, 10, 18, 2, 40, tzinfo=UTC) + timedelta(minutes=n)
        fake_inventory.events.append(_event(f"7b7b7b7b-0000-4000-8000-{n:012d}", "moved", eid,
                                            at.strftime("%Y-%m-%dT%H:%M:%SZ"), label="laptop", frm="USOS",
                                            to="NOD1", catalog_id="laptop"))
    fake_inventory.reviews = [
        {"review_id": "8d8d8d8d-0000-4000-8000-000000000001", "kind": "possible_duplicate", "entity_id": LAPTOP_DUP,
         "candidate_ids": [LAPTOPS[0][0]], "candidates": [], "image_url": None, "description": "laptop",
         "observed_at": "2026-10-18T03:28:19Z", "location_id": "NAUKA", "model_guess": None, "probabilities": {},
         "status": "open", "subject": {"label": "laptop"}},
    ]


#: The five earlier turns of the round-4 standard conversation, replies as the deployed agent gave them.
STANDARD_HISTORY = [
    ("Where is the laptop right now?",
     ("I have found multiple laptops in the following locations:\n1. laptop: US Orbital Segment (USOS), 03:30 UTC\n"
     "2. laptop: Nauka MLM (NAUKA), 03:28 UTC\n3. laptop: Zvezda Service Module (ZVEZDA), 03:26 UTC\n"
     "4. laptop: Cupola (CUP1), 03:21 UTC\n5. laptop: US Orbital Segment (USOS), 03:15 UTC")),
    ("Where is the multimeter?",
     "The Fluke multimeter was last seen in Destiny (US Lab) (LAB1) at 2026-10-18 03:05 UTC."),
    ("Who used the drill?",
     "The pistol-grip drill was used by person (blue shirt, dark shorts) at 2026-10-18 03:10 UTC."),
    ("What changed in the last hour?",
     ("Changes between 2026-10-18 02:32 and 2026-10-18 03:32 UTC (showing 2-11 of 30):\n"
     "2. 2026-10-18 03:28 UTC: Laptop used by person (grey t-shirt, black shorts)\n"
     "3. 2026-10-18 03:25 UTC: Nikon camera moved\n\nand 19 more; ask 'show more changes' to continue.")),
    ("Are there any unknown objects in Node 1?",
     ("There are 2 unknown objects in Node 1 Unity (NOD1):\n1. unknown grey strap: NOD1, 03:00 UTC\n"
     "2. unknown white bag: NOD1, 03:00 UTC")),
]


def conversation(question: str, history=STANDARD_HISTORY) -> list[dict[str, str]]:
    """The standard conversation's earlier turns followed by ``question`` (the next user turn)."""
    out: list[dict[str, str]] = []
    for q, a in history:
        out += [{"role": "user", "content": q}, {"role": "assistant", "content": a}]
    return out + [{"role": "user", "content": question}]
