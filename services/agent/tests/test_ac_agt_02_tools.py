"""AC-AGT-2: typed, read-only tools, each calling inventory-api.

Interface (decided by these tests, see docs/tdd-log/agent.md):
- ``Toolbox(inventory_client, memory).call(name, arguments)`` -> ``ToolResult(name, ok, data, error, error_kind,
  citations)`` with ``has_data``; it never raises for bad input.
- ``Toolbox.specs()`` -> OpenAI tool specs generated from pydantic argument models.
- Citations are ``{"entity_id", "at"}`` or ``{"window_id", "at"}``.
"""
from __future__ import annotations

import json

import pytest

from nasa_agent.inventory_client import InventoryClient, InventoryError
from nasa_agent.tools import READ_TOOLS
from tests.fakes import (
    E_CAMERA,
    E_DRILL,
    E_MERGED,
    E_UNK_LAB,
    E_UNK_NOD2,
    INVENTORY_URL,
    MISSING_ID,
    W_DRILL,
    W_LUNCH,
    FakeInventory,
)

EXPECTED_READ = {
    "search_items", "get_item", "item_history", "where_is", "who_moved", "recent_changes", "list_unknowns",
    "inventory_stats", "search_observations", "recall_memory", "lookup_catalog",
}


def blob(value) -> str:
    """Everything a tool returned, as one string, for shape-agnostic content assertions."""
    return json.dumps(value, default=str)


def cited(result, key="entity_id") -> list[str]:
    return [c[key] for c in result.citations if c.get(key)]


# ---------------------------------------------------------------- specs / schemas
def test_ac_agt_02_read_tool_names_constant():
    assert set(READ_TOOLS) == EXPECTED_READ


def test_ac_agt_02_specs_cover_every_read_tool_plus_propose_update(toolbox):
    specs = toolbox.specs()
    names = [s["function"]["name"] for s in specs]
    assert set(names) == EXPECTED_READ | {"propose_update"}
    assert len(names) == len(set(names))
    assert set(toolbox.names()) == set(names)


def test_ac_agt_02_specs_are_openai_function_specs_with_object_schemas(toolbox):
    for spec in toolbox.specs():
        assert spec["type"] == "function"
        fn = spec["function"]
        assert isinstance(fn.get("description"), str) and fn["description"].strip()
        assert fn["parameters"]["type"] == "object"


def test_ac_agt_02_spec_required_arguments_are_typed(toolbox):
    by_name = {s["function"]["name"]: s["function"]["parameters"] for s in toolbox.specs()}
    assert "name" in by_name["where_is"]["required"]
    assert "name" in by_name["who_moved"]["required"]
    assert "since" not in by_name["who_moved"].get("required", [])
    assert "entity_id" in by_name["get_item"]["required"]
    assert "entity_id" in by_name["item_history"]["required"]
    assert "q" in by_name["search_items"]["required"]
    assert "q" in by_name["search_observations"]["required"]
    assert not by_name["inventory_stats"].get("required")
    assert "location" not in by_name["list_unknowns"].get("required", [])
    assert set(by_name["recall_memory"]["required"]) >= {"scope", "key"}
    assert "entity" in json.dumps(by_name["recall_memory"]) and "global" in json.dumps(by_name["recall_memory"])


def test_ac_agt_02_no_raw_sql_tool(toolbox):
    for name in toolbox.names():
        assert "sql" not in name.lower() and "query" not in name.lower()


# ---------------------------------------------------------------- search_items
async def test_ac_agt_02_search_items_by_text(toolbox, fake_inventory):
    r = await toolbox.call("search_items", {"q": "drill"})
    assert r.ok and r.has_data
    assert E_DRILL in blob(r.data)
    assert E_MERGED not in blob(r.data)
    req = fake_inventory.calls("/items")[-1]
    assert fake_inventory.params(req)["q"] == ["drill"]
    assert E_DRILL in cited(r)


async def test_ac_agt_02_search_items_passes_filters(toolbox, fake_inventory):
    r = await toolbox.call("search_items", {"q": "unknown", "filters": {"location": "NOD2", "status": ["unknown"]}})
    assert r.ok
    params = fake_inventory.params(fake_inventory.calls("/items")[-1])
    assert params["location"] == ["NOD2"]
    assert params["status"] == ["unknown"]
    assert E_UNK_NOD2 in blob(r.data) and E_UNK_LAB not in blob(r.data)


async def test_ac_agt_02_search_items_rejects_bad_filter_enum_without_calling_api(toolbox, fake_inventory):
    r = await toolbox.call("search_items", {"q": "drill", "filters": {"status": ["bogus"]}})
    assert not r.ok and r.error_kind == "validation"
    assert fake_inventory.requests == []


async def test_ac_agt_02_search_items_rejects_out_of_range_confidence(toolbox, fake_inventory):
    r = await toolbox.call("search_items", {"q": "drill", "filters": {"min_confidence": 2}})
    assert not r.ok and r.error_kind == "validation"
    assert fake_inventory.requests == []


async def test_ac_agt_02_search_items_no_match_has_no_data(toolbox):
    r = await toolbox.call("search_items", {"q": "flux capacitor"})
    assert r.ok and not r.has_data
    assert r.citations == []


# ---------------------------------------------------------------- get_item / item_history
async def test_ac_agt_02_get_item(toolbox, fake_inventory):
    r = await toolbox.call("get_item", {"entity_id": E_DRILL})
    assert r.ok and r.has_data
    assert "LAB1S2" in blob(r.data)
    assert fake_inventory.calls(f"/items/{E_DRILL}")
    assert {"entity_id": E_DRILL, "at": "2026-10-01T12:30:00Z"} in [
        {"entity_id": c.get("entity_id"), "at": c.get("at")} for c in r.citations
    ]


async def test_ac_agt_02_get_item_unknown_id_is_not_found_not_crash(toolbox):
    r = await toolbox.call("get_item", {"entity_id": MISSING_ID})
    assert not r.has_data
    assert r.error_kind == "not_found"
    assert r.citations == []


async def test_ac_agt_02_get_item_rejects_non_uuid(toolbox, fake_inventory):
    r = await toolbox.call("get_item", {"entity_id": "../../stats"})
    assert not r.ok and r.error_kind == "validation"
    assert fake_inventory.requests == []


async def test_ac_agt_02_item_history(toolbox, fake_inventory):
    r = await toolbox.call("item_history", {"entity_id": E_DRILL})
    assert r.ok and r.has_data
    assert "crew member in blue shirt" in blob(r.data)
    assert fake_inventory.calls(f"/items/{E_DRILL}/history")
    assert E_DRILL in cited(r)


async def test_ac_agt_02_item_history_unknown_id(toolbox):
    r = await toolbox.call("item_history", {"entity_id": MISSING_ID})
    assert not r.has_data and r.citations == []


# ---------------------------------------------------------------- where_is
async def test_ac_agt_02_where_is_returns_last_location_and_time(toolbox):
    r = await toolbox.call("where_is", {"name": "drill"})
    assert r.ok and r.has_data
    b = blob(r.data)
    assert E_DRILL in b and "LAB1S2" in b and "2026-10-01T12:30" in b
    assert E_MERGED not in b
    assert any(c.get("entity_id") == E_DRILL and str(c.get("at", "")).startswith("2026-10-01T12:30")
               for c in r.citations)


async def test_ac_agt_02_where_is_resolves_catalog_display_name(toolbox):
    r = await toolbox.call("where_is", {"name": "Nikon Z9 camera"})
    assert r.ok and r.has_data
    assert E_CAMERA in blob(r.data) and "NOD2D4" in blob(r.data)


async def test_ac_agt_02_where_is_unknown_name_has_no_data(toolbox):
    r = await toolbox.call("where_is", {"name": "flux capacitor"})
    assert not r.has_data and r.citations == []


@pytest.mark.parametrize("args", [{}, {"name": ""}, {"name": 42}, {"name": "drill", "sql": "DROP TABLE entity"}])
async def test_ac_agt_02_where_is_invalid_arguments(toolbox, fake_inventory, args):
    r = await toolbox.call("where_is", args)
    assert not r.ok and r.error_kind == "validation"
    assert r.error
    assert fake_inventory.requests == []


# ---------------------------------------------------------------- who_moved
async def test_ac_agt_02_who_moved_returns_actor_and_time(toolbox, fake_inventory):
    r = await toolbox.call("who_moved", {"name": "Nikon camera"})
    assert r.ok and r.has_data
    b = blob(r.data)
    assert "crew member, red shirt" in b and "2026-10-01T11:00" in b
    assert any(c.get("entity_id") == E_CAMERA for c in r.citations)
    ev_params = fake_inventory.params(fake_inventory.calls("/events")[-1])
    # default covers every handling type (moved, used, handed_over, ...), filtered client-side: see
    # test_ac_agt_02_who_handled.py and docs/tdd-log/agent.md "who_moved covers handling events"
    assert ev_params.get("type") is None
    assert ev_params.get("entity_id") == [E_CAMERA]


async def test_ac_agt_02_who_moved_since_filters_older_events(toolbox, fake_inventory):
    r = await toolbox.call("who_moved", {"name": "drill", "since": "2026-10-01T00:00:00Z"})
    assert r.ok and r.has_data
    b = blob(r.data)
    assert "crew member in blue shirt" in b
    assert "crew member in green shirt" not in b
    assert "after" in fake_inventory.params(fake_inventory.calls("/events")[-1])


async def test_ac_agt_02_who_moved_without_since_includes_history(toolbox):
    r = await toolbox.call("who_moved", {"name": "drill"})
    b = blob(r.data)
    assert "crew member in blue shirt" in b and "crew member in green shirt" in b


async def test_ac_agt_02_who_moved_rejects_bad_since(toolbox, fake_inventory):
    r = await toolbox.call("who_moved", {"name": "drill", "since": "yesterday-ish"})
    assert not r.ok and r.error_kind == "validation"
    assert fake_inventory.requests == []


async def test_ac_agt_02_who_moved_item_never_moved_has_no_data(toolbox):
    r = await toolbox.call("who_moved", {"name": "unknown white pouch"})
    assert not r.has_data


# ---------------------------------------------------------------- list_unknowns
async def test_ac_agt_02_list_unknowns_by_location(toolbox, fake_inventory):
    r = await toolbox.call("list_unknowns", {"location": "NOD2"})
    assert r.ok and r.has_data
    b = blob(r.data)
    assert E_UNK_NOD2 in b and E_UNK_LAB not in b and E_DRILL not in b
    assert E_UNK_NOD2 in cited(r)


async def test_ac_agt_02_list_unknowns_everywhere(toolbox):
    r = await toolbox.call("list_unknowns", {})
    b = blob(r.data)
    assert E_UNK_NOD2 in b and E_UNK_LAB in b and E_CAMERA not in b


async def test_ac_agt_02_list_unknowns_empty_location(toolbox):
    # AC-AGT-23: COL1 is not a location of the fake dataset (now a validation error); NOD3 exists and has none
    r = await toolbox.call("list_unknowns", {"location": "NOD3"})
    assert r.ok and not r.has_data


# ---------------------------------------------------------------- inventory_stats
async def test_ac_agt_02_inventory_stats(toolbox, fake_inventory):
    r = await toolbox.call("inventory_stats", {})
    assert r.ok and r.has_data
    assert "open_reviews_by_kind" in blob(r.data)
    assert fake_inventory.calls("/stats")


async def test_ac_agt_02_inventory_stats_accepts_null_arguments(toolbox):
    r = await toolbox.call("inventory_stats", None)
    assert r.ok


# ---------------------------------------------------------------- search_observations
async def test_ac_agt_02_search_observations_window_hits_with_window_citations(toolbox, fake_inventory):
    r = await toolbox.call("search_observations", {"q": "drill"})
    assert r.ok and r.has_data
    assert W_DRILL in blob(r.data) and W_LUNCH not in blob(r.data)
    assert fake_inventory.params(fake_inventory.calls("/search")[-1])["q"] == ["drill"]
    win = [c for c in r.citations if c.get("window_id")]
    assert {"window_id": W_DRILL, "at": "2026-10-01T12:19:30Z"} in [
        {"window_id": c["window_id"], "at": c.get("at")} for c in win
    ]
    # windows only: entity hits from /search are not returned by this tool
    assert all(c.get("window_id") for c in r.citations)


async def test_ac_agt_02_search_observations_no_hits(toolbox):
    r = await toolbox.call("search_observations", {"q": "zzzqqq"})
    assert r.ok and not r.has_data


async def test_ac_agt_02_search_observations_rejects_empty_query(toolbox, fake_inventory):
    r = await toolbox.call("search_observations", {"q": ""})
    assert not r.ok and r.error_kind == "validation"
    assert fake_inventory.requests == []


# ---------------------------------------------------------------- recall_memory
async def test_ac_agt_02_recall_memory(toolbox):
    r = await toolbox.call("recall_memory", {"scope": "entity", "key": E_DRILL})
    assert r.ok and r.has_data
    assert "usually kept in LAB1S2" in blob(r.data)


async def test_ac_agt_02_recall_memory_global(toolbox):
    r = await toolbox.call("recall_memory", {"scope": "global", "key": "global"})
    assert r.ok and "GLOBAL-MEMORY" in blob(r.data)


async def test_ac_agt_02_recall_memory_missing_key(toolbox):
    r = await toolbox.call("recall_memory", {"scope": "location", "key": "COL1"})
    assert not r.has_data


async def test_ac_agt_02_recall_memory_rejects_unknown_scope(toolbox):
    r = await toolbox.call("recall_memory", {"scope": "everything", "key": "x"})
    assert not r.ok and r.error_kind == "validation"


# ---------------------------------------------------------------- generic behaviour
async def test_ac_agt_02_arguments_may_be_json_string(toolbox):
    r = await toolbox.call("where_is", json.dumps({"name": "drill"}))
    assert r.ok and E_DRILL in blob(r.data)


async def test_ac_agt_02_malformed_json_arguments_are_validation_errors(toolbox, fake_inventory):
    r = await toolbox.call("where_is", '{"name": "drill"')
    assert not r.ok and r.error_kind == "validation"
    assert fake_inventory.requests == []


async def test_ac_agt_02_unknown_tool(toolbox, fake_inventory):
    r = await toolbox.call("run_sql", {"sql": "SELECT 1"})
    assert not r.ok and r.error_kind == "unknown_tool"
    assert fake_inventory.requests == []


async def test_ac_agt_02_upstream_failure_is_reported_not_raised(toolbox, fake_inventory):
    fake_inventory.fail_with = 500
    r = await toolbox.call("where_is", {"name": "drill"})
    assert not r.ok and r.error_kind == "upstream"
    assert not r.has_data and r.citations == []


@pytest.mark.parametrize("name,args", [
    ("search_items", {"q": "drill"}), ("get_item", {"entity_id": E_DRILL}), ("item_history", {"entity_id": E_DRILL}),
    ("where_is", {"name": "drill"}), ("who_moved", {"name": "camera"}), ("list_unknowns", {}),
    ("inventory_stats", {}), ("search_observations", {"q": "drill"}),
])
async def test_ac_agt_02_read_tools_only_issue_get_requests(toolbox, fake_inventory, name, args):
    r = await toolbox.call(name, args)
    assert r.ok, r.error
    assert fake_inventory.requests, f"{name} must call inventory-api"
    assert fake_inventory.methods == {"GET"}
    assert all(str(req.url).startswith(INVENTORY_URL) for req in fake_inventory.requests)


# ---------------------------------------------------------------- inventory client
async def test_ac_agt_02_inventory_client_uses_base_url_and_returns_none_on_404():
    fake = FakeInventory()
    client = InventoryClient(INVENTORY_URL, transport=fake.transport)
    assert await client.get_item(MISSING_ID) is None
    assert str(fake.requests[-1].url) == f"{INVENTORY_URL}/items/{MISSING_ID}"
    page = await client.list_items({"q": "drill"})
    assert page["total"] >= 1


async def test_ac_agt_02_inventory_client_raises_on_5xx():
    fake = FakeInventory()
    fake.fail_with = 503
    client = InventoryClient(INVENTORY_URL, transport=fake.transport)
    with pytest.raises(InventoryError):
        await client.stats()

