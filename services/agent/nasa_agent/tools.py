"""Typed, read-only agent tools over inventory-api (AC-AGT-2, AC-AGT-11) plus the propose-only write tool (AC-AGT-5).

Every tool argument model is pydantic; the OpenAI tool specs are generated from those models. There is no raw
database tool (research/05): every read goes through inventory-api, memory through ``MemoryStore``.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, ValidationError, model_validator

from nasa_agent.inventory_client import InventoryError

log = logging.getLogger("nasa_agent.tools")

READ_TOOLS: tuple[str, ...] = (
    "search_items",
    "get_item",
    "item_history",
    "where_is",
    "who_moved",
    "list_unknowns",
    "inventory_stats",
    "search_observations",
    "recall_memory",
    "lookup_catalog",
)
WRITE_TOOLS: tuple[str, ...] = ("propose_update",)
#: Tools whose data are citable inventory facts and may ground an answer (AC-AGT-4). inventory_stats (bare counts),
#: recall_memory (stale background) and propose_update (a pending suggestion) never ground a statement.
#: lookup_catalog grounds catalog facts (usual stowage, expected count, fungibility; AC-AGT-11).
GROUNDING_TOOLS = frozenset(
    {
        "search_items",
        "get_item",
        "item_history",
        "where_is",
        "who_moved",
        "list_unknowns",
        "search_observations",
        "lookup_catalog",
    }
)
#: PATCH /items fields that the contract allows to be null.
NULLABLE_PROPOSAL_FIELDS = frozenset({"catalog_id"})
LOCATIONS_TTL_S = 300.0
#: GET /catalog is cached like GET /locations (AC-AGT-11).
CATALOG_TTL_S = LOCATIONS_TTL_S
#: lookup_catalog limits (token budget and prompt-injection surface; AC-AGT-11).
MAX_CATALOG_RESULTS = 20
MAX_CATALOG_ID = 64
#: CatalogItem.catalog_id pattern from contracts/openapi/inventory-api.yaml; other rows are skipped.
CATALOG_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,63}$")
MAX_CATALOG_TEXT = 200
MAX_SYNONYMS = 10
MAX_SYNONYM_TEXT = 100
CATALOG_SOURCE = "catalog"

#: Entities that are no longer "live" inventory; never returned as an answer to "where is X".
HIDDEN_STATUSES = frozenset({"merged", "dismissed"})
ITEM_FIELDS = (
    "entity_id",
    "label",
    "catalog_id",
    "catalog_name",
    "kind",
    "status",
    "last_location",
    "last_location_name",
    "last_seen_at",
    "confidence",
    "needs_review",
    "review_reason",
    "last_moved_by",
    "last_moved_at",
    "description",
)
MAX_RESOLVE = 5

Status = Literal["confirmed", "provisional", "unknown", "merged", "dismissed"]


# ---------------------------------------------------------------- argument models
class _Args(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ItemFilters(_Args):
    """Optional filters for GET /items."""

    kind: Literal["object", "person"] | None = None
    status: list[Status] | None = Field(None, description="Entity statuses to include")
    catalog_id: str | None = Field(None, min_length=1, max_length=64)
    location: str | None = Field(
        None, min_length=1, max_length=64, description="location_id; matches the location and its descendants"
    )
    min_confidence: float | None = Field(None, ge=0, le=1)
    seen_after: AwareDatetime | None = None
    seen_before: AwareDatetime | None = None
    needs_review: bool | None = None
    sort: Literal["last_seen_at", "-last_seen_at", "label", "-label", "confidence", "-confidence"] | None = None
    limit: int | None = Field(None, ge=1, le=100)


class SearchItemsArgs(_Args):
    """Search inventory entities by text with optional filters."""

    q: str = Field(..., min_length=1, max_length=200, description="Free text: label, description or catalog name")
    filters: ItemFilters | None = None


class EntityArgs(_Args):
    entity_id: uuid.UUID = Field(..., description="Entity UUID")


class NameArgs(_Args):
    name: str = Field(..., min_length=1, max_length=200, description="Item label or catalog name, e.g. 'drill'")


#: Event types that count as someone handling an item (contract event.event_type minus plain `seen` sightings).
HANDLING_EVENT_TYPES: tuple[str, ...] = (
    "moved", "used", "handed_over", "added", "removed", "stowed", "unstowed", "consumed",
)
HandlingEventType = Literal["moved", "used", "handed_over", "added", "removed", "stowed", "unstowed", "consumed"]


class WhoMovedArgs(NameArgs):
    since: AwareDatetime | None = Field(None, description="Only events at or after this ISO-8601 time")
    event_types: list[HandlingEventType] | None = Field(
        None, min_length=1, max_length=8, description="Only these handling event types (default: all of them)"
    )


class ListUnknownsArgs(_Args):
    location: str | None = Field(None, min_length=1, max_length=64, description="Module or zone location_id")


class NoArgs(_Args):
    pass


class SearchObservationsArgs(_Args):
    q: str = Field(
        ..., min_length=1, max_length=200, description="Full-text query over scene descriptions and transcripts"
    )


class RecallMemoryArgs(_Args):
    scope: Literal["entity", "location", "global"]
    key: str = Field(
        ...,
        min_length=1,
        max_length=128,
        description="entity_id for scope=entity, module location_id for scope=location, 'global'",
    )


class LookupCatalogArgs(_Args):
    q: str | None = Field(
        None,
        min_length=1,
        max_length=200,
        description="Case-insensitive match on class name, display name or any synonym, e.g. 'multimeter'",
    )
    category: Literal["object", "person", "container"] | None = Field(None, description="Catalog category")
    is_fungible: bool | None = Field(
        None, description="true = tracked as stock counts per zone (food packs, wipes); false = individual items"
    )


class ProposalFields(BaseModel):
    """Fields of PATCH /items/{id} the agent may propose (operator/note are set by the confirming operator)."""

    model_config = ConfigDict(extra="forbid", strict=True)

    label: str | None = Field(None, min_length=1, max_length=200)
    description: str | None = Field(None, max_length=2000)
    catalog_id: str | None = None
    last_location: str | None = Field(None, min_length=1, max_length=64)
    status: Literal["confirmed", "provisional", "unknown", "dismissed"] | None = None
    attributes: dict[str, Any] | None = None
    needs_review: bool | None = None

    @model_validator(mode="after")
    def _not_empty(self) -> ProposalFields:
        if not self.model_fields_set:
            raise ValueError("fields must contain at least one change")
        nulls = sorted(f for f in self.model_fields_set - NULLABLE_PROPOSAL_FIELDS if getattr(self, f) is None)
        if nulls:
            raise ValueError(f"{', '.join(nulls)} cannot be null (only catalog_id may be cleared)")
        return self


class ProposeUpdateArgs(_Args):
    entity_id: uuid.UUID = Field(..., description="Entity UUID to change")
    fields: ProposalFields = Field(..., description="Proposed changes (PATCH /items fields)")


# ---------------------------------------------------------------- result
@dataclass
class ToolResult:
    name: str
    ok: bool
    data: Any = None
    error: str | None = None
    #: "validation" | "unknown_tool" | "not_found" | "conflict" | "upstream" | None
    error_kind: str | None = None
    #: [{"entity_id": ..., "at": ...}], [{"window_id": ..., "at": ...}] or [{"catalog_id": ..., "at"?: ...}]
    citations: list[dict[str, Any]] = field(default_factory=list)

    @property
    def has_data(self) -> bool:
        """True when the tool succeeded and returned something non-empty (grounding, AC-AGT-4)."""
        return self.ok and self.data not in (None, [], {}, "")

    def to_message_payload(self) -> dict[str, Any]:
        """JSON-able payload fed back to the LLM as the ``role: tool`` content."""
        if self.ok:
            return {"ok": True, "data": self.data, "citations": self.citations}
        return {"ok": False, "error": self.error, "error_kind": self.error_kind}


class _NotFound(Exception):
    pass


class _Conflict(Exception):
    pass


class _Invalid(Exception):
    pass


@dataclass(frozen=True)
class _ToolDef:
    name: str
    description: str
    model: type[BaseModel]
    handler: Callable[[Any], Awaitable[tuple[Any, list[dict[str, Any]]]]]
    read_only: bool = True


def format_validation_error(exc: ValidationError) -> str:
    """Compact, model-readable validation message: ``field: problem; ...`` (input values are not echoed)."""
    parts = []
    for err in exc.errors():
        loc = ".".join(str(p) for p in err.get("loc", ())) or "arguments"
        parts.append(f"{loc}: {err.get('msg', 'invalid')}")
    return "invalid arguments - " + "; ".join(parts)


def _jsonable(value: Any) -> Any:
    return json.loads(json.dumps(value, default=str))


def _slim_item(item: dict[str, Any]) -> dict[str, Any]:
    return {k: item.get(k) for k in ITEM_FIELDS if k in item}


def _cite_entity(entity_id: Any, at: Any) -> dict[str, Any] | None:
    if not entity_id or not at:
        return None
    return {"entity_id": str(entity_id), "at": str(at)}


def _dedup(citations: list[dict[str, Any] | None]) -> list[dict[str, Any]]:
    seen: set[tuple[Any, Any, Any]] = set()
    out = []
    for c in citations:
        if not c:
            continue
        key = citation_key(c)
        if key not in seen:
            seen.add(key)
            out.append(c)
    return out


def citation_key(c: dict[str, Any]) -> tuple[Any, Any, Any, Any]:
    """Dedup key of a citation: (entity_id, window_id, catalog_id, at)."""
    return (c.get("entity_id"), c.get("window_id"), c.get("catalog_id"), c.get("at"))


def _cap(value: Any, limit: int) -> str | None:
    if not isinstance(value, str):
        return None
    return value[:limit]


_WORD_SPLIT = re.compile(r"[a-z0-9]+")
_Q_STOPWORDS = frozenset({"the", "a", "an", "of", "for"})


def _catalog_matches(entry: dict[str, Any], q: str) -> bool:
    """Case-insensitive: ``q`` is a substring of class_name, display_name or a synonym, or every word of ``q`` occurs
    in those fields."""
    fields = [entry.get("class_name") or "", entry.get("display_name") or "", *entry.get("synonyms", [])]
    lowered = [f.lower() for f in fields]
    needle = q.strip().lower()
    if any(needle in f for f in lowered):
        return True
    words = [w for w in _WORD_SPLIT.findall(needle) if w not in _Q_STOPWORDS]
    haystack = set(_WORD_SPLIT.findall(" ".join(lowered)))
    return bool(words) and all(w in haystack for w in words)


def _slim_catalog(row: Any) -> dict[str, Any] | None:
    """Bounded catalog entry (free text is operator-entered: capped, never trusted). None for a malformed row."""
    if not isinstance(row, dict):
        return None
    cid = row.get("catalog_id")
    if not isinstance(cid, str) or not CATALOG_ID_RE.fullmatch(cid):
        return None
    synonyms = row.get("synonyms") if isinstance(row.get("synonyms"), list) else []
    expected = row.get("expected_count")
    home = row.get("home_location")
    return {
        "catalog_id": cid,
        "class_name": _cap(row.get("class_name"), MAX_CATALOG_TEXT),
        "display_name": _cap(row.get("display_name"), MAX_CATALOG_TEXT),
        "synonyms": [s[:MAX_SYNONYM_TEXT] for s in synonyms if isinstance(s, str)][:MAX_SYNONYMS],
        "category": _cap(row.get("category"), 32),
        "is_fungible": row.get("is_fungible") if isinstance(row.get("is_fungible"), bool) else None,
        "expected_count": expected if isinstance(expected, int) and not isinstance(expected, bool) else None,
        "home_location": home if isinstance(home, str) and 0 < len(home) <= MAX_CATALOG_ID else None,
        "created_at": row.get("created_at") if isinstance(row.get("created_at"), str) else None,
    }


def _live(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [i for i in items if i.get("status") not in HIDDEN_STATUSES]


def _iso(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC).isoformat()


class Toolbox:
    """The agent's tool surface. ``call`` validates arguments and never raises for bad input."""

    def __init__(self, inventory, memory):
        self.inventory = inventory
        self.memory = memory
        self._tools: dict[str, _ToolDef] = {t.name: t for t in self._definitions()}
        self._locations: tuple[float, dict[str, str | None]] | None = None
        self._catalog: tuple[float, list[dict[str, Any]]] | None = None
        #: monotonic clock for the GET /locations and GET /catalog caches (replaceable in tests).
        self.clock: Callable[[], float] = time.monotonic

    # ------------------------------------------------------------ registry
    def _definitions(self) -> list[_ToolDef]:
        return [
            _ToolDef(
                "search_items",
                "Search inventory items (objects and person-tracks) by text, with optional "
                "filters (kind, status, catalog_id, location, min_confidence, time range, needs_review).",
                SearchItemsArgs,
                self._search_items,
            ),
            _ToolDef(
                "get_item",
                "Get one inventory item by entity_id: last location, time, status, confidence, "
                "recent sightings and events.",
                EntityArgs,
                self._get_item,
            ),
            _ToolDef(
                "item_history",
                "Timeline of sightings and events (moves, uses) for one item.",
                EntityArgs,
                self._item_history,
            ),
            _ToolDef(
                "where_is",
                "Resolve an item name (label, catalog name or synonym) and return its last known "
                "location and the time it was last seen.",
                NameArgs,
                self._where_is,
            ),
            _ToolDef(
                "who_moved",
                "Who moved, used, handed over, stowed or otherwise handled an item (actor described by "
                "role/appearance), what they did, from where to where and when; optionally only since a given "
                "time or only some event_types.",
                WhoMovedArgs,
                self._who_moved,
            ),
            _ToolDef(
                "list_unknowns",
                "List unknown (unclassified) objects, optionally in one module or zone.",
                ListUnknownsArgs,
                self._list_unknowns,
            ),
            _ToolDef(
                "inventory_stats",
                "Inventory counts by status, kind and module, and open review items.",
                NoArgs,
                self._inventory_stats,
            ),
            _ToolDef(
                "search_observations",
                "Full-text search over observation windows (scene descriptions and "
                "speech transcripts). Returns window ids and times.",
                SearchObservationsArgs,
                self._search_observations,
            ),
            _ToolDef(
                "recall_memory",
                "Read a background memory summary (scope entity, location or global). "
                "Summaries may be stale; prefer live tools for current facts.",
                RecallMemoryArgs,
                self._recall_memory,
            ),
            _ToolDef(
                "lookup_catalog",
                "Look up the item CATALOG (reference data, not sightings): where an item is usually kept "
                "(home_location), how many are expected on board (expected_count), whether it is tracked as stock "
                "counts (is_fungible), its synonyms and category. Filter by q (class name, display name or synonym), "
                "category or is_fungible; no arguments lists the catalog. At most 20 entries are returned (sorted by "
                "catalog_id), so narrow the filters for long lists. For where an item is NOW use where_is.",
                LookupCatalogArgs,
                self._lookup_catalog,
            ),
            _ToolDef(
                "propose_update",
                "Propose a correction to an item (label, description, catalog_id, "
                "last_location, status, attributes, needs_review). Returns a pending proposal that an operator "
                "must confirm in the dashboard; nothing is changed by this tool.",
                ProposeUpdateArgs,
                self._propose_update,
                read_only=False,
            ),
        ]

    def names(self) -> list[str]:
        return list(self._tools)

    def is_read_only(self, name: str) -> bool:
        return self._tools[name].read_only

    def argument_model(self, name: str) -> type[BaseModel]:
        return self._tools[name].model

    def description(self, name: str) -> str:
        return self._tools[name].description

    def parameters_schema(self, name: str) -> dict[str, Any]:
        schema = self._tools[name].model.model_json_schema()
        schema.pop("title", None)
        schema.setdefault("properties", {})
        schema["type"] = "object"
        return schema

    def specs(self) -> list[dict[str, Any]]:
        """OpenAI-format tool specs: [{"type":"function","function":{"name","description","parameters"}}]."""
        return [
            {
                "type": "function",
                "function": {
                    "name": name,
                    "description": self.description(name),
                    "parameters": self.parameters_schema(name),
                },
            }
            for name in self._tools
        ]

    # ------------------------------------------------------------ dispatch
    def _parse(self, name: str, arguments: Any) -> BaseModel | ToolResult:
        tool = self._tools.get(name)
        if tool is None:
            return ToolResult(
                name,
                False,
                error=f"unknown tool {name!r}; available: {', '.join(self._tools)}",
                error_kind="unknown_tool",
            )
        if arguments is None or arguments == "":
            raw: Any = {}
        elif isinstance(arguments, str):
            try:
                raw = json.loads(arguments)
            except ValueError as exc:
                return ToolResult(
                    name, False, error=f"invalid arguments - not valid JSON ({exc.msg})", error_kind="validation"
                )
        else:
            raw = arguments
        if not isinstance(raw, dict):
            return ToolResult(name, False, error="invalid arguments - expected a JSON object", error_kind="validation")
        try:
            return tool.model.model_validate(raw)
        except ValidationError as exc:
            return ToolResult(name, False, error=format_validation_error(exc), error_kind="validation")

    def validate(self, name: str, arguments: Any) -> ToolResult | None:
        """Static validation only (no I/O): the error ``ToolResult`` for an unknown tool or invalid arguments, else
        None. Lets the harness check a whole LLM step before running any of its calls."""
        parsed = self._parse(name, arguments)
        return parsed if isinstance(parsed, ToolResult) else None

    async def call(self, name: str, arguments: str | dict[str, Any] | None) -> ToolResult:
        """Validate ``arguments`` (JSON string or dict) with the tool's pydantic model and run it. Never raises for
        bad input: validation errors, unknown tools and upstream failures come back as ``ok=False``."""
        args = self._parse(name, arguments)
        if isinstance(args, ToolResult):
            return args
        tool = self._tools[name]
        try:
            data, citations = await tool.handler(args)
        except _NotFound as exc:
            return ToolResult(name, False, error=str(exc) or "not found", error_kind="not_found")
        except _Conflict as exc:
            return ToolResult(name, False, error=str(exc), error_kind="conflict")
        except _Invalid as exc:
            return ToolResult(name, False, error=f"invalid arguments - {exc}", error_kind="validation")
        except InventoryError as exc:
            log.warning("upstream failure", extra={"tool": name, "error": str(exc)})
            return ToolResult(name, False, error=f"upstream unavailable: {exc}", error_kind="upstream")
        except Exception as exc:  # a tool must never crash the chat loop
            log.exception("tool failed", extra={"tool": name})
            return ToolResult(name, False, error=f"tool failed: {type(exc).__name__}", error_kind="upstream")
        data = _jsonable(data)
        return ToolResult(name, True, data=data, citations=_dedup(citations) if data not in (None, [], {}) else [])

    # ------------------------------------------------------------ helpers
    async def location_names(self) -> dict[str, str | None]:
        """{location_id: display_name} from GET /locations, cached for ``LOCATIONS_TTL_S`` seconds."""
        now = self.clock()
        if self._locations is None or now - self._locations[0] > LOCATIONS_TTL_S:
            rows = await self.inventory.locations()
            names = {
                str(r["location_id"]): _cap(r.get("display_name"), MAX_CATALOG_TEXT)
                for r in rows
                if isinstance(r, dict) and r.get("location_id")
            }
            self._locations = (now, names)
        return self._locations[1]

    async def known_locations(self) -> frozenset[str]:
        """location ids from GET /locations, cached for ``LOCATIONS_TTL_S`` seconds."""
        return frozenset(await self.location_names())

    async def catalog(self) -> list[dict[str, Any]]:
        """Bounded entries of GET /catalog, cached for ``CATALOG_TTL_S`` seconds (a failed fetch is not cached)."""
        now = self.clock()
        if self._catalog is None or now - self._catalog[0] > CATALOG_TTL_S:
            rows = await self.inventory.catalog()
            if not isinstance(rows, list):
                raise InventoryError("inventory-api GET /catalog returned a non-list body")
            entries = [e for e in (_slim_catalog(r) for r in rows) if e is not None]
            self._catalog = (now, entries)
        return self._catalog[1]

    async def _resolve(self, name: str, *, limit: int = MAX_RESOLVE) -> list[dict[str, Any]]:
        page = await self.inventory.list_items({"q": name, "limit": limit})
        return _live(page.get("items", []))[:limit]

    # ------------------------------------------------------------ handlers
    async def _search_items(self, a: SearchItemsArgs):
        params: list[tuple[str, Any]] = [("q", a.q)]
        f = a.filters
        if f is not None:
            for key, value in f.model_dump(exclude_none=True).items():
                if key == "status":
                    params += [("status", s) for s in value]
                elif isinstance(value, bool):
                    params.append((key, "true" if value else "false"))
                elif isinstance(value, datetime):
                    params.append((key, _iso(value)))
                else:
                    params.append((key, value))
        if not any(k == "limit" for k, _ in params):
            params.append(("limit", 20))
        page = await self.inventory.list_items(params)
        explicit_status = f is not None and f.status
        items = page.get("items", [])
        if not explicit_status:
            items = _live(items)
        data = [_slim_item(i) for i in items]
        return data, [_cite_entity(i.get("entity_id"), i.get("last_seen_at")) for i in data]

    async def _get_item(self, a: EntityArgs):
        item = await self.inventory.get_item(str(a.entity_id))
        if item is None:
            raise _NotFound(f"no item with entity_id {a.entity_id}")
        data = _slim_item(item)
        data["merged_into"] = item.get("merged_into")
        data["events"] = [
            {
                k: e.get(k)
                for k in (
                    "event_id",
                    "event_type",
                    "actor_desc",
                    "from_location",
                    "to_location",
                    "occurred_at",
                    "confidence",
                )
            }
            for e in (item.get("events") or [])[:10]
        ]
        data["sightings"] = [
            {k: s.get(k) for k in ("location_id", "seen_at", "confidence", "description") if k in s}
            for s in (item.get("sightings") or [])[:10]
        ]
        return data, [_cite_entity(item.get("entity_id"), item.get("last_seen_at"))]

    async def _item_history(self, a: EntityArgs):
        timeline = await self.inventory.item_history(str(a.entity_id))
        if timeline is None:
            raise _NotFound(f"no item with entity_id {a.entity_id}")
        data = [{k: t.get(k) for k in ("at", "kind", "location_id", "summary")} for t in timeline[:50]]
        return data, [_cite_entity(a.entity_id, t.get("at")) for t in data[:10]]

    async def _where_is(self, a: NameArgs):
        items = await self._resolve(a.name)
        data = [
            {
                k: i.get(k)
                for k in (
                    "entity_id",
                    "label",
                    "catalog_name",
                    "status",
                    "last_location",
                    "last_location_name",
                    "last_seen_at",
                    "confidence",
                    "last_moved_by",
                    "last_moved_at",
                )
            }
            for i in items
        ]
        return data, [_cite_entity(i["entity_id"], i.get("last_seen_at")) for i in data]

    async def _who_moved(self, a: WhoMovedArgs):
        items = await self._resolve(a.name)
        data = []
        citations = []
        for item in items:
            wanted = set(a.event_types or HANDLING_EVENT_TYPES)
            # GET /events takes one `type`: ask for it when only one is wanted, else filter here.
            params: dict[str, Any] = {"entity_id": item["entity_id"], "limit": 50}
            if len(wanted) == 1:
                params["type"] = next(iter(wanted))
            if a.since is not None:
                params["after"] = _iso(a.since)
            events = [e for e in await self.inventory.list_events(params) if e.get("event_type") in wanted][:20]
            if not events:
                continue
            moves = [
                {
                    k: e.get(k)
                    for k in (
                        "event_id",
                        "event_type",
                        "actor_desc",
                        "from_location",
                        "to_location",
                        "occurred_at",
                        "confidence",
                        "source",
                    )
                }
                for e in events
            ]
            data.append(
                {
                    "entity_id": item["entity_id"],
                    "label": item.get("label"),
                    "last_location": item.get("last_location"),
                    "moves": moves,
                }
            )
            citations += [_cite_entity(item["entity_id"], m.get("occurred_at")) for m in moves]
        return data, citations

    async def _list_unknowns(self, a: ListUnknownsArgs):
        params: dict[str, Any] = {"status": "unknown", "limit": 100}
        if a.location:
            params["location"] = a.location
        page = await self.inventory.list_items(params)
        items = [i for i in page.get("items", []) if i.get("status") == "unknown"]
        data = [
            {
                k: i.get(k)
                for k in (
                    "entity_id",
                    "label",
                    "description",
                    "last_location",
                    "last_location_name",
                    "last_seen_at",
                    "confidence",
                    "needs_review",
                    "review_reason",
                )
            }
            for i in items
        ]
        return data, [_cite_entity(i["entity_id"], i.get("last_seen_at")) for i in data]

    async def _inventory_stats(self, a: NoArgs):
        return await self.inventory.stats(), []

    async def _search_observations(self, a: SearchObservationsArgs):
        hits = await self.inventory.search(a.q, limit=20)
        windows = [h for h in hits if h.get("type") == "window" and h.get("id")]
        data = [
            {"window_id": h["id"], "at": h.get("time"), "snippet": h.get("snippet"), "camera_id": h.get("camera_id")}
            for h in windows
        ]
        return data, [{"window_id": str(w["window_id"]), "at": str(w["at"])} if w.get("at") else None for w in data]

    async def _recall_memory(self, a: RecallMemoryArgs):
        row = await asyncio.to_thread(self.memory.get, a.scope, a.key)
        return row, []

    async def _lookup_catalog(self, a: LookupCatalogArgs):
        entries = await self.catalog()
        if a.q is not None:
            entries = [e for e in entries if _catalog_matches(e, a.q)]
        if a.category is not None:
            entries = [e for e in entries if e["category"] == a.category]
        if a.is_fungible is not None:
            entries = [e for e in entries if e["is_fungible"] is a.is_fungible]
        entries = entries[:MAX_CATALOG_RESULTS]
        if not entries:
            return [], []
        try:
            names = await self.location_names()
        except InventoryError as exc:  # display names are optional; the catalog answer still stands
            log.warning("locations unavailable for lookup_catalog", extra={"error": str(exc)})
            names = {}
        data, citations = [], []
        for e in entries:
            home = e["home_location"]
            data.append(
                {
                    **{k: v for k, v in e.items() if k != "created_at"},
                    "home_location_name": names.get(home) if home else None,
                    "source": CATALOG_SOURCE,
                }
            )
            cite: dict[str, Any] = {"catalog_id": e["catalog_id"]}
            if e["created_at"]:
                cite["at"] = e["created_at"]
            citations.append(cite)
        return data, citations

    async def _propose_update(self, a: ProposeUpdateArgs):
        item = await self.inventory.get_item(str(a.entity_id))
        if item is None:
            raise _NotFound(f"no item with entity_id {a.entity_id}")
        if item.get("status") == "merged":
            raise _Conflict(f"entity {a.entity_id} is merged into {item.get('merged_into')}; edit that entity")
        location = a.fields.last_location
        if location is not None and location not in await self.known_locations():
            raise _Invalid(f"fields.last_location: unknown location id {location!r} (see the ISS location list)")
        proposal = {
            "proposal_id": str(uuid.uuid4()),
            "entity_id": str(a.entity_id),
            "fields": a.fields.model_dump(mode="json", exclude_unset=True),
            "status": "pending",
            "current": {k: item.get(k) for k in ("label", "status", "last_location", "catalog_id")},
        }
        return proposal, [_cite_entity(item.get("entity_id"), item.get("last_seen_at"))]
