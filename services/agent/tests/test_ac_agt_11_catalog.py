"""AC-AGT-11: ``lookup_catalog(q?, category?, is_fungible?)`` reads GET /catalog (cluster integration finding).

Interface (decided by these tests, see docs/tdd-log/agent.md):
- entries carry catalog_id, class_name, display_name, synonyms, category, is_fungible, expected_count, home_location,
  home_location_name (GET /locations display_name, None when unknown) and ``source == "catalog"``;
- citations are ``{"catalog_id", "at"}`` (``at`` = catalog ``created_at``), never entity_id/window_id;
- GET /catalog and GET /locations are cached for LOCATIONS_TTL_S; ``Toolbox.clock`` is the monotonic clock used.
"""
from __future__ import annotations

import json

import pytest

from nasa_agent.tools import GROUNDING_TOOLS, LOCATIONS_TTL_S, READ_TOOLS
from tests.fakes import CATALOG_CREATED_AT, INVENTORY_URL, _cat

CATALOG_FIELDS = {
    "catalog_id", "class_name", "display_name", "synonyms", "category", "is_fungible", "expected_count",
    "home_location", "home_location_name",
}


def ids(result) -> list[str]:
    return [e["catalog_id"] for e in result.data or []]


# ---------------------------------------------------------------- spec / registry
def test_ac_agt_11_lookup_catalog_is_a_read_tool():
    assert "lookup_catalog" in READ_TOOLS


def test_ac_agt_11_lookup_catalog_is_a_grounding_tool():
    assert "lookup_catalog" in GROUNDING_TOOLS


def test_ac_agt_11_tool_spec_in_openai_tool_list(toolbox):
    specs = {s["function"]["name"]: s for s in toolbox.specs()}
    assert "lookup_catalog" in specs
    spec = specs["lookup_catalog"]
    assert spec["type"] == "function"
    fn = spec["function"]
    desc = fn["description"].lower()
    assert "catalog" in desc and ("usual" in desc or "normally" in desc) and "expected" in desc
    params = fn["parameters"]
    assert params["type"] == "object"
    assert {"q", "category", "is_fungible"} <= set(params["properties"])
    assert not params.get("required")
    assert set(json.loads(json.dumps(params)).get("properties")) == {"q", "category", "is_fungible"}
    blob = json.dumps(params["properties"]["category"])
    assert "object" in blob and "container" in blob and "person" in blob
    assert toolbox.is_read_only("lookup_catalog")


# ---------------------------------------------------------------- argument validation
@pytest.mark.parametrize("args", [
    {"q": ""}, {"q": 5}, {"q": "x" * 201}, {"category": "food"}, {"is_fungible": "maybe"},
    {"q": "drill", "sql": "SELECT 1"}, {"limit": 500},
])
async def test_ac_agt_11_invalid_arguments_rejected_without_api_call(toolbox, fake_inventory, args):
    r = await toolbox.call("lookup_catalog", args)
    assert not r.ok and r.error_kind == "validation" and r.error
    assert fake_inventory.requests == []


async def test_ac_agt_11_null_or_empty_arguments_list_the_catalog(toolbox, fake_inventory):
    for args in (None, {}, "{}"):
        r = await toolbox.call("lookup_catalog", args)
        assert r.ok and r.has_data, r.error
        assert ids(r) == sorted(e["catalog_id"] for e in fake_inventory.catalog)


# ---------------------------------------------------------------- filtering
async def test_ac_agt_11_q_matches_class_name_case_insensitive(toolbox, fake_inventory):
    r = await toolbox.call("lookup_catalog", {"q": "MULTIMETER"})
    assert r.ok and ids(r) == ["multimeter"]
    assert fake_inventory.calls("/catalog") and fake_inventory.methods == {"GET"}
    assert all(str(req.url).startswith(INVENTORY_URL) for req in fake_inventory.requests)


async def test_ac_agt_11_q_matches_display_name(toolbox):
    r = await toolbox.call("lookup_catalog", {"q": "Nikon Z9"})
    assert ids(r) == ["camera-dslr"]


@pytest.mark.parametrize("q,expected", [("volt meter", "multimeter"), ("pgt", "drill-pistol"),
                                        ("Towelettes", "wipes"), ("retort pouch", "food-pack")])
async def test_ac_agt_11_q_matches_synonyms(toolbox, q, expected):
    r = await toolbox.call("lookup_catalog", {"q": q})
    assert ids(r) == [expected]


async def test_ac_agt_11_q_no_match_has_no_data(toolbox):
    r = await toolbox.call("lookup_catalog", {"q": "flux capacitor"})
    assert r.ok and not r.has_data
    assert r.citations == []


async def test_ac_agt_11_filter_by_category(toolbox):
    r = await toolbox.call("lookup_catalog", {"category": "container"})
    assert ids(r) == ["ctb-half", "ziploc"]


async def test_ac_agt_11_filter_by_is_fungible(toolbox):
    r = await toolbox.call("lookup_catalog", {"is_fungible": True})
    assert ids(r) == ["food-pack", "wipes", "ziploc"]
    r = await toolbox.call("lookup_catalog", {"is_fungible": False})
    assert "multimeter" in ids(r) and not {"food-pack", "wipes", "ziploc"} & set(ids(r))


async def test_ac_agt_11_filters_combine(toolbox):
    r = await toolbox.call("lookup_catalog", {"category": "container", "is_fungible": True})
    assert ids(r) == ["ziploc"]
    r = await toolbox.call("lookup_catalog", {"q": "bag", "is_fungible": False})
    assert ids(r) == ["ctb-half"]


# ---------------------------------------------------------------- returned fields / citations
async def test_ac_agt_11_entry_fields_and_home_location_display_name(toolbox):
    r = await toolbox.call("lookup_catalog", {"q": "multimeter"})
    (entry,) = r.data
    assert CATALOG_FIELDS <= set(entry)
    assert entry["home_location"] == "LAB1"
    assert entry["home_location_name"] == "Destiny (US Lab)"
    assert entry["expected_count"] == 1
    assert entry["is_fungible"] is False
    assert entry["synonyms"] == ["Fluke", "volt meter"]
    assert entry["source"] == "catalog"
    assert "reference_image_url" not in entry


async def test_ac_agt_11_home_location_name_null_when_unknown_or_unset(toolbox):
    r = await toolbox.call("lookup_catalog", {"category": "container"})
    by_id = {e["catalog_id"]: e for e in r.data}
    assert by_id["ctb-half"]["home_location"] == "PMM1" and by_id["ctb-half"]["home_location_name"] is None
    assert by_id["ziploc"]["home_location"] is None and by_id["ziploc"]["home_location_name"] is None


async def test_ac_agt_11_citations_are_catalog_ids(toolbox):
    r = await toolbox.call("lookup_catalog", {"is_fungible": True})
    # AC-AGT-17 adds an optional display `label`; the identity fields are unchanged
    ids = [{k: v for k, v in c.items() if k != "label"} for c in r.citations]
    assert ids == [{"catalog_id": c, "at": CATALOG_CREATED_AT} for c in ("food-pack", "wipes", "ziploc")]
    assert all("entity_id" not in c and "window_id" not in c for c in r.citations)


async def test_ac_agt_11_citation_without_created_at_has_no_at(toolbox, fake_inventory):
    for row in fake_inventory.catalog:
        row.pop("created_at")
    r = await toolbox.call("lookup_catalog", {"q": "multimeter"})
    assert [{k: v for k, v in c.items() if k != "label"} for c in r.citations] == [{"catalog_id": "multimeter"}]


async def test_ac_agt_11_upstream_failure_reported(toolbox, fake_inventory):
    fake_inventory.fail_with = 503
    r = await toolbox.call("lookup_catalog", {"q": "multimeter"})
    assert not r.ok and r.error_kind == "upstream" and r.citations == []


async def test_ac_agt_11_locations_outage_still_answers_without_names(toolbox, fake_inventory):
    original = fake_inventory.handler

    def handler(request):
        if request.url.path == "/locations":
            fake_inventory.requests.append(request)
            import httpx
            return httpx.Response(503, json={"detail": "down"})
        return original(request)

    fake_inventory.transport.handler = handler
    r = await toolbox.call("lookup_catalog", {"q": "multimeter"})
    assert r.ok and r.data[0]["home_location"] == "LAB1" and r.data[0]["home_location_name"] is None


# ---------------------------------------------------------------- caching (same TTL as locations)
async def test_ac_agt_11_catalog_and_locations_cached_for_ttl(toolbox, fake_inventory):
    now = [1000.0]
    toolbox.clock = lambda: now[0]
    await toolbox.call("lookup_catalog", {"q": "multimeter"})
    await toolbox.call("lookup_catalog", {"q": "drill"})
    now[0] += LOCATIONS_TTL_S - 1
    await toolbox.call("lookup_catalog", {"is_fungible": True})
    assert len(fake_inventory.calls("/catalog")) == 1
    assert len(fake_inventory.calls("/locations")) == 1
    fake_inventory.catalog.append(_cat("zz-new", "new thing", "New thing", []))
    now[0] += 2
    r = await toolbox.call("lookup_catalog", {"q": "new thing"})
    assert ids(r) == ["zz-new"]
    assert len(fake_inventory.calls("/catalog")) == 2
    assert len(fake_inventory.calls("/locations")) == 2


async def test_ac_agt_11_failed_fetch_is_not_cached(toolbox, fake_inventory):
    fake_inventory.fail_with = 500
    assert (await toolbox.call("lookup_catalog", {"q": "multimeter"})).error_kind == "upstream"
    fake_inventory.fail_with = None
    assert ids(await toolbox.call("lookup_catalog", {"q": "multimeter"})) == ["multimeter"]


# ---------------------------------------------------------------- size limits (token budget, injection surface)
async def test_ac_agt_11_result_count_capped(toolbox, fake_inventory):
    fake_inventory.catalog = [_cat(f"item-{i:03d}", "thing", f"Thing {i}", []) for i in range(60)]
    r = await toolbox.call("lookup_catalog", {})
    assert r.ok and 1 <= len(r.data) <= 20
    assert len(r.citations) == len(r.data)


async def test_ac_agt_11_free_text_fields_capped(toolbox, fake_inventory):
    fake_inventory.catalog = [_cat("huge", "huge " + "x" * 5000, "Huge " + "y" * 5000,
                                   [f"syn{i} " + "z" * 500 for i in range(100)], expected_count=3)]
    r = await toolbox.call("lookup_catalog", {"q": "huge"})
    (entry,) = r.data
    assert len(entry["display_name"]) <= 200 and len(entry["class_name"]) <= 200
    assert len(entry["synonyms"]) <= 10 and all(len(s) <= 100 for s in entry["synonyms"])
    assert len(json.dumps(entry)) < 3000


async def test_ac_agt_11_malformed_catalog_rows_are_skipped(toolbox, fake_inventory):
    fake_inventory.catalog = [
        "not an object",
        {"display_name": "no id"},
        {"catalog_id": "x" * 500, "class_name": "long id", "display_name": "long id"},
        _cat("multimeter", "multimeter", "Fluke multimeter", ["Fluke"], expected_count=1, home_location="LAB1"),
    ]
    r = await toolbox.call("lookup_catalog", {})
    assert r.ok and ids(r) == ["multimeter"]


# ---------------------------------------------------------------- review round (AC-AGT-11)
async def test_ac_agt_11_review_catalog_ids_must_match_the_contract_pattern(toolbox, fake_inventory):
    fake_inventory.catalog = [
        _cat("NOD3-KIT", "kit", "Kit", []),
        _cat("kit 09:00", "kit", "Kit", []),
        _cat("-kit", "kit", "Kit", []),
        _cat("kit-1", "kit", "Kit", []),
    ]
    r = await toolbox.call("lookup_catalog", {"q": "kit"})
    assert ids(r) == ["kit-1"]


def test_ac_agt_11_review_description_states_result_limit(toolbox):
    assert "20" in toolbox.description("lookup_catalog")
