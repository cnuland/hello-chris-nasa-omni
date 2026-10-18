"""AC-AGT-17: citations carry optional label/location/location_name/catalog_id (UI review UI-06: chips read
"Item da9709e7 · 22:51 UTC" and the operator cannot tell five laptops apart)."""
from __future__ import annotations

from nasa_agent.tools import citation_key
from tests.conftest import user
from tests.fakes import E_DRILL, E_UNK_NOD2, FakeLLM, assistant_text, assistant_tools, tool_call


def _cite(r, eid):
    return next(c for c in r.citations if c.get("entity_id") == eid)


async def test_ac_agt_17_where_is_citation_has_label_and_location(toolbox):
    r = await toolbox.call("where_is", {"name": "drill"})
    c = _cite(r, E_DRILL)
    assert c["label"] == "pistol-grip drill"
    assert c["location"] == "LAB1S2" and c["location_name"] == "Lab starboard rack 2"
    assert c["catalog_id"] == "drill-pistol"
    assert c["at"] == "2026-10-01T12:30:00Z"


async def test_ac_agt_17_search_and_unknowns_citations_enriched(toolbox):
    r = await toolbox.call("search_items", {"q": "drill"})
    assert _cite(r, E_DRILL)["label"] == "pistol-grip drill"
    r = await toolbox.call("list_unknowns", {})
    c = _cite(r, E_UNK_NOD2)
    assert c["label"] == "unknown white pouch" and c["location"] == "NOD2O4"
    assert "catalog_id" not in c, "no catalog -> key omitted"


async def test_ac_agt_17_get_item_and_who_moved_citations_enriched(toolbox):
    r = await toolbox.call("get_item", {"entity_id": E_DRILL})
    assert _cite(r, E_DRILL)["label"] == "pistol-grip drill"
    r = await toolbox.call("who_moved", {"name": "drill"})
    c = _cite(r, E_DRILL)
    assert c["label"] == "pistol-grip drill" and c["location"] == "LAB1S2"


async def test_ac_agt_17_catalog_citation_has_label(toolbox):
    r = await toolbox.call("lookup_catalog", {"q": "multimeter"})
    assert r.citations[0]["catalog_id"] == "multimeter" and r.citations[0]["label"] == "Fluke multimeter"
    assert "location" not in r.citations[0], "a home location is not where the item is"


async def test_ac_agt_17_label_text_is_capped_and_collapsed(toolbox, fake_inventory):
    fake_inventory.items[0]["label"] = "drill\n\n" + "very long label " * 20
    r = await toolbox.call("where_is", {"name": "drill"})
    label = _cite(r, E_DRILL)["label"]
    assert "\n" not in label and len(label) <= 80


def test_ac_agt_17_dedup_key_unchanged():
    a = {"entity_id": "e", "at": "t", "label": "x"}
    b = {"entity_id": "e", "at": "t", "label": "y", "location": "LAB1"}
    assert citation_key(a) == citation_key(b)


async def test_ac_agt_17_rich_citations_reach_the_response(make_harness):
    llm = FakeLLM([assistant_tools(tool_call("where_is", {"name": "drill"})),
                   assistant_text("The pistol-grip drill was last seen in LAB1S2 at 12:30Z.")])
    res = await make_harness(llm).run(user("where is the drill?"))
    c = next(c for c in res["citations"] if c.get("entity_id") == E_DRILL)
    assert c["label"] == "pistol-grip drill" and c["location"] == "LAB1S2"
