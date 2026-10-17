"""AC-AGT-5 write proposals: ``propose_update(entity_id, fields)`` returns a proposal object; the agent never calls
PATCH itself (the dashboard confirms).

Proposal object (decided here): {proposal_id (uuid), entity_id, fields, status: "pending"}. Allowed ``fields`` are the
PATCH /items/{id} body fields minus ``operator``/``note``: label, description, catalog_id, last_location, status
(confirmed|provisional|unknown|dismissed), attributes, needs_review. The chat response lists proposals under
``proposals``.
"""
from __future__ import annotations

import json
import uuid

import pytest

from tests.conftest import user
from tests.fakes import E_DRILL, MISSING_ID, FakeLLM, assistant_text, assistant_tools, tool_call


async def test_ac_agt_05_propose_update_returns_pending_proposal(toolbox, fake_inventory):
    r = await toolbox.call("propose_update", {"entity_id": E_DRILL, "fields": {"last_location": "NOD2D4"}})
    assert r.ok and r.has_data
    p = r.data
    assert uuid.UUID(p["proposal_id"])
    assert p["entity_id"] == E_DRILL
    assert p["fields"] == {"last_location": "NOD2D4"}
    assert p["status"] == "pending"
    assert fake_inventory.methods <= {"GET"}, "propose_update must never write to inventory-api"


async def test_ac_agt_05_proposal_ids_are_unique(toolbox):
    a = await toolbox.call("propose_update", {"entity_id": E_DRILL, "fields": {"label": "drill A"}})
    b = await toolbox.call("propose_update", {"entity_id": E_DRILL, "fields": {"label": "drill A"}})
    assert a.data["proposal_id"] != b.data["proposal_id"]


async def test_ac_agt_05_multiple_fields(toolbox):
    fields = {"label": "cordless drill", "status": "confirmed", "needs_review": False, "attributes": {"color": "grey"}}
    r = await toolbox.call("propose_update", {"entity_id": E_DRILL, "fields": fields})
    assert r.ok and r.data["fields"] == fields


@pytest.mark.parametrize("fields", [
    {},                                   # nothing to change
    {"status": "merged"},                 # merges go through /merge, not PATCH
    {"bogus": 1},                         # not a PATCH field
    {"operator": "agent"},                # the agent must not impersonate the operator
    {"last_location": 7},                 # wrong type
])
async def test_ac_agt_05_invalid_fields_are_rejected(toolbox, fake_inventory, fields):
    r = await toolbox.call("propose_update", {"entity_id": E_DRILL, "fields": fields})
    assert not r.ok and r.error_kind == "validation"
    assert fake_inventory.methods <= {"GET"}


async def test_ac_agt_05_invalid_entity_id_rejected(toolbox):
    r = await toolbox.call("propose_update", {"entity_id": "drill", "fields": {"label": "x"}})
    assert not r.ok and r.error_kind == "validation"


async def test_ac_agt_05_proposal_for_missing_entity_is_not_found(toolbox, fake_inventory):
    r = await toolbox.call("propose_update", {"entity_id": MISSING_ID, "fields": {"label": "x"}})
    assert not r.ok and r.error_kind == "not_found"
    assert fake_inventory.methods <= {"GET"}


async def test_ac_agt_05_chat_turn_surfaces_proposal_without_patching(make_harness, fake_inventory):
    llm = FakeLLM([
        assistant_tools(tool_call("propose_update", {"entity_id": E_DRILL, "fields": {"last_location": "NOD2D4"}})),
        assistant_text("I proposed moving the drill to NOD2D4; please confirm in the dashboard."),
    ])
    res = await make_harness(llm).run(user("the drill is actually in Node 2 deck bay 4, fix it"))
    assert len(res["proposals"]) == 1
    p = res["proposals"][0]
    assert p["entity_id"] == E_DRILL and p["fields"] == {"last_location": "NOD2D4"} and p["status"] == "pending"
    assert fake_inventory.methods <= {"GET"}
    assert not any(r.method == "PATCH" for r in fake_inventory.requests)


async def test_ac_agt_05_no_proposals_key_is_empty_list_when_none(make_harness):
    llm = FakeLLM([assistant_tools(tool_call("where_is", {"name": "drill"})), assistant_text("LAB1S2.")])
    res = await make_harness(llm).run(user("where is the drill?"))
    assert res["proposals"] == []


async def test_ac_agt_05_no_direct_write_tool_exists(toolbox):
    names = set(toolbox.names())
    writes = {n for n in names if any(w in n for w in ("patch", "update", "delete", "merge", "create", "write"))}
    assert writes == {"propose_update"}
    spec = next(s for s in toolbox.specs() if s["function"]["name"] == "propose_update")
    assert "fields" in spec["function"]["parameters"]["required"]
    assert "entity_id" in spec["function"]["parameters"]["required"]
    assert "propos" in json.dumps(spec).lower()
