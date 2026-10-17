"""AC-AGT-5 proposal validation (review round 1).

- Only ``catalog_id`` may be proposed as null (PATCH /items contract); explicit nulls elsewhere are validation errors.
- ``last_location`` must be a known location id (GET /locations).
- Proposing a change to a merged entity is a ``conflict`` (contract: PATCH on a merged entity is 409).
"""
from __future__ import annotations

import pytest

from tests.fakes import E_DRILL, E_MERGED


@pytest.mark.parametrize("field", ["label", "description", "last_location", "status", "attributes", "needs_review"])
async def test_ac_agt_05_explicit_null_rejected_for_non_nullable_fields(toolbox, field):
    r = await toolbox.call("propose_update", {"entity_id": E_DRILL, "fields": {field: None}})
    assert not r.ok and r.error_kind == "validation", r


async def test_ac_agt_05_null_catalog_id_is_allowed(toolbox):
    r = await toolbox.call("propose_update", {"entity_id": E_DRILL, "fields": {"catalog_id": None}})
    assert r.ok and r.data["fields"] == {"catalog_id": None}


async def test_ac_agt_05_unknown_location_rejected(toolbox, fake_inventory):
    r = await toolbox.call("propose_update", {"entity_id": E_DRILL, "fields": {"last_location": "NOWHERE-ZONE"}})
    assert not r.ok and r.error_kind == "validation"
    assert "NOWHERE-ZONE" in r.error or "location" in r.error
    assert fake_inventory.methods <= {"GET"}


async def test_ac_agt_05_known_zone_and_module_accepted(toolbox):
    for loc in ("NOD2D4", "LAB1"):
        r = await toolbox.call("propose_update", {"entity_id": E_DRILL, "fields": {"last_location": loc}})
        assert r.ok, (loc, r)


async def test_ac_agt_05_location_check_when_inventory_down_is_upstream_error(toolbox, fake_inventory):
    fake_inventory.fail_with = 503
    r = await toolbox.call("propose_update", {"entity_id": E_DRILL, "fields": {"last_location": "NOD2D4"}})
    assert not r.ok and r.error_kind == "upstream"


async def test_ac_agt_05_merged_entity_is_conflict(toolbox, fake_inventory):
    r = await toolbox.call("propose_update", {"entity_id": E_MERGED, "fields": {"label": "x"}})
    assert not r.ok and r.error_kind == "conflict"
    assert fake_inventory.methods <= {"GET"}
