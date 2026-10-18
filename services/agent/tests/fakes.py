"""Deterministic fakes for agent tests: a scripted LLM and an in-memory inventory-api (httpx.MockTransport).

Nothing here talks to a real model or a real inventory-api.
"""
from __future__ import annotations

import json
from copy import deepcopy
from typing import Any
from urllib.parse import parse_qs

import httpx

INVENTORY_URL = "http://inventory.test"
LLM_URL = "http://llm.test/v1"

# ---------------------------------------------------------------- fixed inventory dataset
E_DRILL = "11111111-1111-4111-8111-111111111111"
E_CAMERA = "22222222-2222-4222-8222-222222222222"
E_UNK_NOD2 = "33333333-3333-4333-8333-333333333333"
E_UNK_LAB = "44444444-4444-4444-8444-444444444444"
E_MERGED = "55555555-5555-4555-8555-555555555555"
E_PERSON = "66666666-6666-4666-8666-666666666666"
W_DRILL = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
W_LUNCH = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
EV_CAM_MOVE = "cccccccc-cccc-4ccc-8ccc-cccccccccccc"
EV_DRILL_MOVE = "dddddddd-dddd-4ddd-8ddd-dddddddddddd"
EV_DRILL_MOVE_OLD = "eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee"
R_UNK = "99999999-9999-4999-8999-999999999999"

MISSING_ID = "00000000-0000-4000-8000-00000000dead"


def _item(entity_id, label, *, catalog_id=None, catalog_name=None, kind="object", status="confirmed",
          last_location=None, last_location_name=None, last_seen_at=None, confidence=0.9, needs_review=False,
          last_moved_by=None, last_moved_at=None, description=None):
    return {
        "entity_id": entity_id, "catalog_id": catalog_id, "catalog_name": catalog_name, "kind": kind,
        "status": status, "label": label, "description": description, "attributes": {},
        "last_location": last_location, "last_location_name": last_location_name, "last_seen_at": last_seen_at,
        "first_seen_at": last_seen_at, "confidence": confidence, "snapshot_url": f"/media/snap/{entity_id}.jpg",
        "needs_review": needs_review, "review_reason": "unknown_object" if needs_review else None,
        "last_moved_by": last_moved_by, "last_moved_at": last_moved_at,
    }


def default_items() -> list[dict[str, Any]]:
    return [
        _item(E_DRILL, "pistol-grip drill", catalog_id="drill-pistol", catalog_name="Pistol-grip drill",
              last_location="LAB1S2", last_location_name="Lab starboard rack 2", last_seen_at="2026-10-01T12:30:00Z",
              last_moved_by="crew member in blue shirt", last_moved_at="2026-10-01T12:20:00Z"),
        _item(E_CAMERA, "Nikon camera", catalog_id="camera-dslr", catalog_name="Nikon Z9 camera",
              last_location="NOD2D4", last_location_name="Node 2 deck bay 4", last_seen_at="2026-10-01T11:05:00Z",
              last_moved_by="crew member, red shirt", last_moved_at="2026-10-01T11:00:00Z"),
        _item(E_UNK_NOD2, "unknown white pouch", status="unknown", last_location="NOD2O4",
              last_location_name="Node 2 overhead bay 4", last_seen_at="2026-10-01T10:00:00Z", confidence=0.3,
              needs_review=True),
        _item(E_UNK_LAB, "unknown grey box", status="unknown", last_location="LAB1P3",
              last_location_name="Lab port rack 3", last_seen_at="2026-10-01T09:00:00Z", confidence=0.2,
              needs_review=True),
        _item(E_MERGED, "drill duplicate", catalog_id="drill-pistol", status="merged", last_location="LAB1S2",
              last_seen_at="2026-10-01T08:00:00Z"),
        _item(E_PERSON, "crew member, blue shirt", kind="person", catalog_id="crew-member", catalog_name="Crew member",
              last_location="LAB1", last_seen_at="2026-10-01T12:31:00Z"),
    ]


def default_events() -> list[dict[str, Any]]:
    return [
        {"event_id": EV_DRILL_MOVE, "event_type": "moved", "entity_id": E_DRILL,
         "actor_desc": "crew member in blue shirt", "from_location": "LAB1P3", "to_location": "LAB1S2",
         "occurred_at": "2026-10-01T12:20:00Z", "confidence": 0.85, "evidence_url": "/media/ev/1.jpg",
         "source": "pipeline"},
        {"event_id": EV_DRILL_MOVE_OLD, "event_type": "moved", "entity_id": E_DRILL,
         "actor_desc": "crew member in green shirt", "from_location": "NOD2D4", "to_location": "LAB1P3",
         "occurred_at": "2026-09-30T08:00:00Z", "confidence": 0.8, "evidence_url": "/media/ev/0.jpg",
         "source": "pipeline"},
        {"event_id": EV_CAM_MOVE, "event_type": "moved", "entity_id": E_CAMERA,
         "actor_desc": "crew member, red shirt", "from_location": "LAB1P3", "to_location": "NOD2D4",
         "occurred_at": "2026-10-01T11:00:00Z", "confidence": 0.9, "evidence_url": "/media/ev/2.jpg",
         "source": "pipeline"},
    ]


def default_reviews() -> list[dict[str, Any]]:
    return [
        {"review_id": R_UNK, "kind": "unknown_object", "entity_id": E_UNK_NOD2, "candidate_ids": [], "candidates": [],
         "image_url": "/media/r/1.jpg", "description": "white pouch with velcro", "observed_at": "2026-10-01T10:00:00Z",
         "location_id": "NOD2O4", "model_guess": None, "probabilities": {}, "status": "open"},
    ]


def default_windows() -> list[dict[str, Any]]:
    return [
        {"type": "window", "id": W_DRILL, "snippet": "Crew member stows the <b>drill</b> in the lab rack",
         "time": "2026-10-01T12:19:30Z", "camera_id": "cam-lab1-fwd"},
        {"type": "window", "id": W_LUNCH, "snippet": "Crew eating lunch in Node 1", "time": "2026-10-01T12:00:00Z",
         "camera_id": "cam-nod2-aft"},
    ]


STATS = {
    "by_status": {"confirmed": 3, "provisional": 0, "unknown": 2, "merged": 1, "dismissed": 0},
    "by_kind": {"object": 4, "person": 1},
    "by_module": {"LAB1": 3, "NOD2": 2},
    "open_reviews_by_kind": {"unknown_object": 1, "possible_duplicate": 0, "low_confidence": 0, "conflict": 0},
}

HIDDEN = {"merged", "dismissed"}

LOCATIONS = [
    {"location_id": "USOS", "parent_id": None, "kind": "segment", "display_name": "US Orbital Segment"},
    {"location_id": "LAB1", "parent_id": "USOS", "kind": "module", "display_name": "Destiny (US Lab)"},
    {"location_id": "NOD1", "parent_id": "USOS", "kind": "module", "display_name": "Node 1 Unity"},
    {"location_id": "NOD2", "parent_id": "USOS", "kind": "module", "display_name": "Node 2 Harmony"},
    {"location_id": "NOD3", "parent_id": "USOS", "kind": "module", "display_name": "Node 3 Tranquility"},
    {"location_id": "LAB1S2", "parent_id": "LAB1", "kind": "zone", "display_name": "Lab starboard rack 2"},
    {"location_id": "LAB1P3", "parent_id": "LAB1", "kind": "zone", "display_name": "Lab port rack 3"},
    {"location_id": "NOD2D4", "parent_id": "NOD2", "kind": "zone", "display_name": "Node 2 deck bay 4"},
    {"location_id": "NOD2O4", "parent_id": "NOD2", "kind": "zone", "display_name": "Node 2 overhead bay 4"},
]


def _cat(catalog_id, class_name, display_name, synonyms, category="object", is_fungible=False, expected_count=None,
         home_location=None):
    return {
        "catalog_id": catalog_id, "class_name": class_name, "display_name": display_name, "synonyms": list(synonyms),
        "category": category, "is_fungible": is_fungible, "expected_count": expected_count,
        "home_location": home_location, "reference_image_url": None, "created_at": CATALOG_CREATED_AT,
    }


CATALOG_CREATED_AT = "2026-09-01T00:00:00Z"


def default_catalog() -> list[dict[str, Any]]:
    """Subset of contracts/db/002_seed.sql catalog_item rows, shaped like GET /catalog (sorted by catalog_id)."""
    rows = [
        _cat("camera-dslr", "camera", "Nikon Z9 camera", ["Nikon", "DSLR", "still camera"], expected_count=4,
             home_location="NOD2"),
        _cat("crew-member", "person", "Crew member", ["astronaut", "cosmonaut", "crew", "person"], category="person",
             expected_count=7),
        _cat("ctb-half", "cargo transfer bag", "Cargo Transfer Bag (half)", ["CTB", "half CTB", "stowage bag"],
             category="container", home_location="PMM1"),
        _cat("drill-pistol", "drill", "Pistol-grip drill", ["power drill", "PGT", "pistol grip tool", "driver"],
             expected_count=2, home_location="LAB1"),
        _cat("food-pack", "food package", "Rehydratable food pack", ["food pouch", "retort pouch", "meal pack"],
             is_fungible=True, home_location="NOD1"),
        _cat("multimeter", "multimeter", "Fluke multimeter", ["Fluke", "volt meter"], expected_count=1,
             home_location="LAB1"),
        _cat("wipes", "wipes", "Wet wipes", ["cleaning wipes", "towelettes"], is_fungible=True),
        _cat("ziploc", "ziploc bag", "Ziploc bag", ["zip bag", "plastic bag"], category="container", is_fungible=True),
    ]
    return sorted(rows, key=lambda r: r["catalog_id"])


class FakeInventory:
    """In-memory stand-in for inventory-api. Use ``.transport`` with ``InventoryClient(..., transport=...)``.

    Records every request in ``.requests``; ``fail_with`` forces a status code for every request.
    """

    def __init__(self):
        self.items = default_items()
        self.events = default_events()
        self.reviews = default_reviews()
        self.windows = default_windows()
        self.stats = deepcopy(STATS)
        self.catalog = default_catalog()
        self.locations = deepcopy(LOCATIONS)
        self.requests: list[httpx.Request] = []
        self.fail_with: int | None = None
        #: simulate an inventory-api without the `types=` filter on GET /events (422 on unknown query use)
        self.reject_types = False
        #: when True GET /events adds entity_label (the newer API); default mirrors the deployed API (no label)
        self.event_labels = False
        #: server-side page cap for GET /items (None = honour `limit`); exercises offset paging
        self.max_page: int | None = None
        self.transport = httpx.MockTransport(self.handler)

    # -- helpers for assertions
    @property
    def methods(self) -> set[str]:
        return {r.method for r in self.requests}

    def calls(self, path: str) -> list[httpx.Request]:
        return [r for r in self.requests if r.url.path == path]

    def params(self, request: httpx.Request) -> dict[str, list[str]]:
        return parse_qs(request.url.query.decode() if isinstance(request.url.query, bytes) else request.url.query)

    # -- routing
    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.fail_with is not None:
            return httpx.Response(self.fail_with, json={"detail": "forced failure"})
        if request.method != "GET":
            return httpx.Response(405, json={"detail": "fake inventory is read-only"})
        path = request.url.path
        q = self.params(request)
        one = lambda k: q.get(k, [None])[0]
        parts = [p for p in path.split("/") if p]
        if path == "/healthz":
            return httpx.Response(200, json={"status": "ok", "db": True})
        if path == "/items":
            return httpx.Response(200, json=self._list_items(q))
        if len(parts) == 2 and parts[0] == "items":
            item = self._find(parts[1])
            if item is None:
                return httpx.Response(404, json={"detail": "not found"})
            detail = deepcopy(item)
            detail["events"] = [e for e in self.events if e["entity_id"] == item["entity_id"]]
            detail["sightings"] = []
            detail["exemplar_count"] = 0
            return httpx.Response(200, json=detail)
        if len(parts) == 3 and parts[0] == "items" and parts[2] == "history":
            item = self._find(parts[1])
            if item is None:
                return httpx.Response(404, json={"detail": "not found"})
            tl = [{"at": e["occurred_at"], "kind": "event", "location_id": e["to_location"],
                   "summary": f"{e['event_type']} by {e['actor_desc']}", "image_url": e["evidence_url"]}
                  for e in self.events if e["entity_id"] == item["entity_id"]]
            tl.sort(key=lambda t: t["at"], reverse=True)
            return httpx.Response(200, json=tl)
        if path == "/events":
            evs = list(self.events)
            if one("entity_id"):
                evs = [e for e in evs if e["entity_id"] == one("entity_id")]
            if one("type"):
                evs = [e for e in evs if e["event_type"] == one("type")]
            if one("types"):
                if self.reject_types:
                    return httpx.Response(422, json={"detail": "unknown query parameter types"})
                wanted = set(one("types").split(","))
                evs = [e for e in evs if e["event_type"] in wanted]
            if one("after"):
                evs = [e for e in evs if e["occurred_at"] >= _z(one("after"))]
            if one("before"):
                evs = [e for e in evs if e["occurred_at"] <= _z(one("before"))]
            evs.sort(key=lambda e: e["occurred_at"], reverse=True)
            evs = deepcopy(evs[: int(one("limit") or 100)])
            if self.event_labels:
                for e in evs:
                    item = self._find(e["entity_id"])
                    e.setdefault("entity_label", item["label"] if item else None)
            return httpx.Response(200, json=evs)
        if path == "/review":
            status = one("status") or "open"
            rv = [r for r in self.reviews if r["status"] == status]
            if one("kind"):
                rv = [r for r in rv if r["kind"] == one("kind")]
            if one("location"):
                rv = [r for r in rv if (r.get("location_id") or "").startswith(one("location"))]
            return httpx.Response(200, json=rv[: int(one("limit") or 50)])
        if path == "/search":
            term = (one("q") or "").lower()
            hits = [{"type": "entity", "id": i["entity_id"], "snippet": i["label"], "time": i["last_seen_at"]}
                    for i in self.items if i["status"] not in HIDDEN and term and term in i["label"].lower()]
            hits += [w for w in self.windows if term and any(t in w["snippet"].lower() for t in term.split())]
            return httpx.Response(200, json=hits[: int(one("limit") or 20)])
        if path == "/stats":
            return httpx.Response(200, json=self.stats)
        if path == "/locations":
            return httpx.Response(200, json=deepcopy(self.locations))
        if path == "/catalog":
            return httpx.Response(200, json=deepcopy(self.catalog))
        return httpx.Response(404, json={"detail": f"no route {path}"})

    def _find(self, entity_id: str) -> dict[str, Any] | None:
        return next((i for i in self.items if i["entity_id"] == entity_id), None)

    def _list_items(self, q: dict[str, list[str]]) -> dict[str, Any]:
        items = list(self.items)
        statuses = q.get("status")
        if statuses:
            items = [i for i in items if i["status"] in statuses]
        else:
            items = [i for i in items if i["status"] not in HIDDEN]
        if q.get("q"):
            term = q["q"][0].lower()
            words = [w for w in term.split() if w not in {"the", "a", "an"}]
            items = [i for i in items
                     if any(w in (i["label"] + " " + (i["catalog_name"] or "") + " " + (i["catalog_id"] or "")).lower()
                            for w in words)]
        if q.get("location"):
            loc = q["location"][0]
            items = [i for i in items if (i["last_location"] or "").startswith(loc)]
        if q.get("kind"):
            items = [i for i in items if i["kind"] == q["kind"][0]]
        if q.get("catalog_id"):
            items = [i for i in items if i["catalog_id"] == q["catalog_id"][0]]
        if q.get("needs_review"):
            want = q["needs_review"][0].lower() == "true"
            items = [i for i in items if i["needs_review"] == want]
        if q.get("seen_after"):
            after = _z(q["seen_after"][0])
            items = [i for i in items if (i["last_seen_at"] or "") >= after]
        if q.get("min_confidence"):
            items = [i for i in items if i["confidence"] >= float(q["min_confidence"][0])]
        items.sort(key=lambda i: i["last_seen_at"] or "", reverse=True)
        limit = int(q.get("limit", ["50"])[0])
        offset = int(q.get("offset", ["0"])[0])
        if self.max_page:
            limit = min(limit, self.max_page)  # simulate a server-side page cap
        return {"items": items[offset:offset + limit], "total": len(items)}


def _z(value: str) -> str:
    return value.replace("+00:00", "Z")


# ---------------------------------------------------------------- scripted LLM
def tool_call(name: str, arguments: dict[str, Any] | str, call_id: str | None = None) -> dict[str, Any]:
    args = arguments if isinstance(arguments, str) else json.dumps(arguments)
    return {"id": call_id or f"call_{name}", "type": "function", "function": {"name": name, "arguments": args}}


def assistant_tools(*calls: dict[str, Any]) -> dict[str, Any]:
    return {"role": "assistant", "content": None, "tool_calls": list(calls)}


def assistant_text(text: str) -> dict[str, Any]:
    return {"role": "assistant", "content": text}


class FakeLLM:
    """Implements the harness LLM protocol. Returns scripted assistant messages in order and records each request.

    When the script is exhausted it repeats ``default`` (None -> AssertionError, to catch runaway loops).
    """

    def __init__(self, script: list[dict[str, Any]], default: dict[str, Any] | None = None):
        self.script = list(script)
        self.default = default
        self.requests: list[dict[str, Any]] = []

    @property
    def calls(self) -> int:
        return len(self.requests)

    async def complete(self, messages, tools, tool_choice="auto"):
        self.requests.append({"messages": deepcopy(messages), "tools": deepcopy(tools), "tool_choice": tool_choice})
        if self.script:
            return deepcopy(self.script.pop(0))
        if self.default is not None:
            return deepcopy(self.default)
        raise AssertionError("FakeLLM script exhausted")


def chat_completion(message: dict[str, Any], model: str = "agent-llm") -> dict[str, Any]:
    """Wrap an assistant message as an OpenAI chat.completion response body."""
    return {
        "id": "chatcmpl-test", "object": "chat.completion", "created": 0, "model": model,
        "choices": [{"index": 0, "message": message,
                     "finish_reason": "tool_calls" if message.get("tool_calls") else "stop"}],
        "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
    }


class FakeLLMServer:
    """OpenAI-compatible /v1/chat/completions over httpx.MockTransport, scripted like FakeLLM."""

    def __init__(self, script: list[dict[str, Any]], status: int = 200):
        self.script = list(script)
        self.status = status
        self.requests: list[httpx.Request] = []
        self.transport = httpx.MockTransport(self.handler)

    def bodies(self) -> list[dict[str, Any]]:
        return [json.loads(r.content) for r in self.requests]

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if request.url.path != "/v1/chat/completions" or request.method != "POST":
            return httpx.Response(404, json={"error": "not found"})
        if self.status != 200:
            return httpx.Response(self.status, json={"error": {"message": "boom"}})
        if not self.script:
            return httpx.Response(500, json={"error": {"message": "script exhausted"}})
        return httpx.Response(200, json=chat_completion(self.script.pop(0)))


class FakeMemory:
    """Duck-typed MemoryStore for tool/harness tests."""

    def __init__(self, rows: dict[tuple[str, str], str] | None = None, global_summary: str | None = None):
        self.rows = dict(rows or {})
        if global_summary is not None:
            self.rows[("global", "global")] = global_summary

    def get(self, scope, key):
        if (scope, key) not in self.rows:
            return None
        return {"scope": scope, "scope_key": key, "summary": self.rows[(scope, key)], "priority": 0.5,
                "source_from": None, "source_to": None, "generated_at": "2026-10-01T12:00:00Z"}

    def get_global(self):
        return self.rows.get(("global", "global"))


class FakeInventoryBlock:
    """Duck-typed InventoryBlockRenderer: returns a fixed block and records the questions it was asked."""

    def __init__(self, text: str = "INVENTORY-BLOCK: E-UNK white pouch open review NOD2O4"):
        self.text = text
        self.questions: list[str] = []

    def render(self, question, token_budget, now=None):
        self.questions.append(question)
        return self.text
