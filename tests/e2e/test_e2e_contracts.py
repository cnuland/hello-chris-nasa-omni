"""Cross-component contract checks on the data of the e2e run (the component suites mock their neighbours).

- inventory-api responses read by the dashboard and the agent validate against the OpenAPI component schemas
  (contracts/openapi/inventory-api.yaml), on real pipeline data rather than hand-made fixtures.
- the agent's ``/chat/stream`` speaks the SSE dialect the dashboard parses (dashboard/src/lib/sse.ts,
  dashboard/src/api/types.ts ChatStreamEvent): ``token`` {text}*, then ``done`` = the /chat response.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml
from jsonschema import Draft202012Validator

from conftest import REPO, tool_call, tool_results

OPENAPI = yaml.safe_load(Path(REPO / "contracts" / "openapi" / "inventory-api.yaml").read_text())


def validator(schema: dict) -> Draft202012Validator:
    """A validator whose root carries ``components`` so ``#/components/schemas/...`` refs resolve."""
    return Draft202012Validator({**schema, "components": OPENAPI["components"]})


def ref(name: str) -> dict:
    return {"$ref": f"#/components/schemas/{name}"}


def errors(schema: dict, instance) -> list[str]:
    return [f"{'/'.join(map(str, e.absolute_path))}: {e.message}" for e in validator(schema).iter_errors(instance)]


@pytest.mark.parametrize("path,params,schema", [
    ("/items", {"limit": 500}, ref("ItemPage")),
    ("/review", {}, {"type": "array", "items": ref("ReviewItem")}),
    ("/events", {"limit": 100}, {"type": "array", "items": ref("Event")}),
    ("/catalog", {}, {"type": "array", "items": ref("CatalogItem")}),
    ("/locations", {}, {"type": "array", "items": ref("Location")}),
])
def test_e2e_contract_list_responses_match_openapi(replay, api, path, params, schema):
    resp = api.get(path, params=params)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body, f"{path} returned nothing to validate"
    assert errors(schema, body) == []


def test_e2e_contract_item_detail_and_history_match_openapi(replay, api):
    drill = api.get("/items", params={"catalog_id": "drill-pistol"}).json()["items"][0]
    detail = api.get(f"/items/{drill['entity_id']}").json()
    assert errors(ref("ItemDetail"), detail) == []
    history = api.get(f"/items/{drill['entity_id']}/history").json()
    assert errors({"type": "array", "items": ref("TimelineEntry")}, history) == []


def test_e2e_contract_review_candidates_expand_to_items(replay, api):
    """AC-API-12/24/25: ``candidates`` are full Items (the dashboard renders them as cards): the stored
    ``candidate_ids`` expanded, or up to 8 server suggestions for a duplicate review with none stored."""
    for review in api.get("/review").json():
        if review["candidates_source"] == "suggested":
            assert review["candidate_ids"] == [] and 0 < len(review["candidates"]) <= 8
        else:
            assert len(review.get("candidates", [])) == len(review.get("candidate_ids", []))


def test_e2e_contract_agent_stream_matches_dashboard_parser(replay, make_agent):
    def policy(messages, tools):
        results = tool_results(messages)
        if not results:
            return tool_call("where_is", {"name": "drill"})
        top = results[-1]["data"][0]
        return {"role": "assistant", "content": f"The drill is in {top['last_location']}."}

    client, _ = make_agent(policy)
    with client.stream("POST", "/chat/stream", json={"messages": [{"role": "user", "content": "where is the drill?"}]},
                       headers={"accept": "text/event-stream"}) as resp:
        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("text/event-stream")
        raw = "".join(resp.iter_text())
    events = []
    for block in raw.split("\n\n"):
        lines = [ln for ln in block.splitlines() if ln and not ln.startswith(":")]
        if not lines:
            continue
        name = next(ln[len("event: "):] for ln in lines if ln.startswith("event: "))
        data = json.loads("\n".join(ln[len("data: "):] for ln in lines if ln.startswith("data: ")))
        events.append((name, data))
    names = [n for n, _ in events]
    # AC-AGT-16: progress `status` events may precede the answer tokens; tokens still join to done.reply
    assert names[-1] == "done" and "token" in names and set(names[:-1]) <= {"status", "token"}
    assert all(d.get("phase") in {"llm", "tool", "checking"} for n, d in events if n == "status")
    done = events[-1][1]
    assert "".join(d["text"] for n, d in events if n == "token") == done["reply"]
    assert done["citations"] and all(c.get("entity_id") and c.get("at") for c in done["citations"])
    assert {"reply", "citations", "tool_calls", "proposals"} <= set(done)
