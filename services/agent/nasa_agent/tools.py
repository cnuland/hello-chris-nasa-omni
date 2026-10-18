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
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, ValidationError, model_validator

from nasa_agent.grounding import scrub_claims
from nasa_agent.inventory_block import _Location, module_aliases
from nasa_agent.inventory_client import InventoryError

log = logging.getLogger("nasa_agent.tools")

READ_TOOLS: tuple[str, ...] = (
    "search_items",
    "get_item",
    "item_history",
    "where_is",
    "who_moved",
    "recent_changes",
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
        "recent_changes",
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
#: who_moved resolves this many matching entities (AC-AGT-14: the 5 newest missed the drill with the actor).
MAX_HANDLER_ENTITIES = 20
MAX_HANDLER_EVENTS = 20
#: recent_changes limits (AC-AGT-13).
RECENT_DEFAULT_WINDOW = timedelta(hours=1)
RECENT_MAX_WINDOW = timedelta(days=7)
MAX_RECENT_CHANGES = 30
MAX_LABEL_LOOKUPS = 10
#: free text (labels, actors) fed to the model / UI: whitespace-collapsed and capped.
MAX_FREE_TEXT = 120
MAX_CITATION_TEXT = 80

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
    location: str | None = Field(
        None, min_length=1, max_length=64, description="Module or zone location_id (or module name, e.g. 'Node 1')"
    )


CHANGE_TYPES: tuple[str, ...] = (*HANDLING_EVENT_TYPES, "new_item", "review_resolved")
ChangeType = Literal[
    "moved", "used", "handed_over", "added", "removed", "stowed", "unstowed", "consumed", "new_item", "review_resolved"
]


class RecentChangesArgs(_Args):
    since: AwareDatetime | None = Field(None, description="ISO-8601 time with zone; default: one hour ago")
    location: str | None = Field(
        None, min_length=1, max_length=64, description="Only changes in this location_id or module name (and below)"
    )
    types: list[ChangeType] | None = Field(None, min_length=1, max_length=10, description="Default: all change types")


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
    q: str | None = Field(None, min_length=1, max_length=200, description="Name or synonym, e.g. 'multimeter'")
    category: Literal["object", "person", "container"] | None = Field(None, description="Catalog category")
    is_fungible: bool | None = Field(None, description="true = stock counts (food packs, wipes)")


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
            # the model gets the identity keys only; the AC-AGT-17 display fields are for the UI (token budget)
            slim = [
                {k: c[k] for k in CITATION_ID_KEYS if k in c and not (k == "catalog_id" and c.get("entity_id"))}
                for c in self.citations
            ]
            return {"ok": True, "data": self.data, "citations": slim}
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


def clean_text(value: Any, limit: int = MAX_FREE_TEXT) -> str | None:
    """Free text from the database (labels, actor descriptions): control characters and runs of whitespace collapsed
    to single spaces, capped at ``limit`` characters. None for non-strings/empty."""
    if not isinstance(value, str):
        return None
    text = " ".join(value.split())
    return text[:limit] or None


def _cite_entity(
    entity_id: Any, at: Any, *, label: Any = None, location: Any = None, location_name: Any = None,
    catalog_id: Any = None,
) -> dict[str, Any] | None:
    """Entity citation {entity_id, at} plus the optional display fields of AC-AGT-17 (omitted when unknown)."""
    if not entity_id or not at:
        return None
    cite: dict[str, Any] = {"entity_id": str(entity_id), "at": str(at)}
    for key, value in (("label", label), ("location", location), ("location_name", location_name),
                       ("catalog_id", catalog_id)):
        text = clean_text(value, MAX_CITATION_TEXT)
        if text:
            cite[key] = text
    return cite


def _cite_item(item: dict[str, Any], at: Any = None, *, location: Any = None, location_name: Any = None):
    return _cite_entity(
        item.get("entity_id"), at if at is not None else item.get("last_seen_at"), label=item.get("label"),
        location=location if location is not None else item.get("last_location"),
        location_name=location_name if location_name is not None else item.get("last_location_name"),
        catalog_id=item.get("catalog_id"),
    )


def _parse_ts(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        dt = datetime.fromisoformat(value)
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def _iso_z(dt: datetime) -> str:
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _strip_titles(schema: Any) -> Any:
    """Drop pydantic's ``title`` annotations (string values only) from a JSON schema: tokens re-sent on every step."""
    if isinstance(schema, dict):
        return {k: _strip_titles(v) for k, v in schema.items() if not (k == "title" and isinstance(v, str))}
    if isinstance(schema, list):
        return [_strip_titles(v) for v in schema]
    return schema


def _compact_optional(schema: Any, keep_nullable: frozenset[str] = frozenset()) -> Any:
    """An optional field (``default: null``) is described by its non-null branch alone and without the default
    (omitting it means "not set"), so ``anyOf: [X, {type: null}]`` becomes X. Properties named in ``keep_nullable``
    (propose_update's catalog_id: null clears it) are left untouched."""
    if isinstance(schema, list):
        return [_compact_optional(v, keep_nullable) for v in schema]
    if not isinstance(schema, dict):
        return schema
    out = {}
    for k, v in schema.items():
        if k == "properties" and isinstance(v, dict):
            out[k] = {p: (pv if p in keep_nullable else _compact_optional(pv, keep_nullable)) for p, pv in v.items()}
        else:
            out[k] = _compact_optional(v, keep_nullable)
    if "default" in out and out["default"] is None:
        out.pop("default")
        branches = out.get("anyOf")
        if isinstance(branches, list):
            rest = [b for b in branches if b != {"type": "null"}]
            if len(rest) == 1 and isinstance(rest[0], dict):
                out.pop("anyOf")
                out = {**rest[0], **out}
    return out


def evidence_view(name: str, data: Any) -> Any:
    """The part of a grounding tool's data that may support claims (AC-AGT-4).

    recent_changes (AC-AGT-13): only the changes count; the query scope (since/until/location) never does (an empty
    window must not ground an invented event), labels are free text and never count, and actor text is scrubbed of
    locations/times. who_moved: actor text is scrubbed the same way. Other tools are unchanged."""
    if name == "recent_changes" and isinstance(data, dict):
        return {
            "changes": [
                {k: (scrub_claims(v) if k == "actor" else v) for k, v in c.items() if k != "label"}
                for c in data.get("changes") or []
                if isinstance(c, dict)
            ]
        }
    if name == "who_moved" and isinstance(data, list):
        return [
            {**e, "moves": [{**m, "actor_desc": scrub_claims(m.get("actor_desc"))} for m in e.get("moves") or []]}
            if isinstance(e, dict) else e
            for e in data
        ]
    return data


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


CITATION_ID_KEYS = ("entity_id", "window_id", "catalog_id", "at")


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
        self._locations: tuple[float, dict[str, _Location]] | None = None
        self._catalog: tuple[float, list[dict[str, Any]]] | None = None
        #: monotonic clock for the GET /locations and GET /catalog caches (replaceable in tests).
        self.clock: Callable[[], float] = time.monotonic
        #: wall clock for relative windows such as recent_changes' default "last hour" (replaceable in tests).
        self.now: Callable[[], datetime] = lambda: datetime.now(UTC)

    # ------------------------------------------------------------ registry
    def _definitions(self) -> list[_ToolDef]:
        return [
            _ToolDef(
                "search_items",
                "Search inventory items (objects and person-tracks) by text, with optional filters.",
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
                "recent_changes",
                "What changed in the last hour / since a time / today: handling events (moved, used, added...), "
                "new items and resolved reviews with label, location, actor and time. since: ISO time (default one "
                "hour ago, max 7 days back).",
                RecentChangesArgs,
                self._recent_changes,
            ),
            _ToolDef(
                "list_unknowns",
                "List unknown (unclassified) objects, optionally in one module or zone (location id or module name).",
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
                "Full-text search over video observation windows (scene descriptions, speech transcripts).",
                SearchObservationsArgs,
                self._search_observations,
            ),
            _ToolDef(
                "recall_memory",
                "Read a background memory summary (may be stale; prefer live tools for current facts).",
                RecallMemoryArgs,
                self._recall_memory,
            ),
            _ToolDef(
                "lookup_catalog",
                "Item CATALOG (reference data, not sightings): where an item is usually kept (home_location), how "
                "many are expected on board (expected_count), stock-counted or not (is_fungible), synonyms, category. "
                "Filter by q, category or is_fungible; at most 20 entries. For where an item is NOW use where_is.",
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
        schema = _strip_titles(self._tools[name].model.model_json_schema())
        schema = _compact_optional(schema, frozenset() if self._tools[name].read_only else NULLABLE_PROPOSAL_FIELDS)
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

    def summarize_args(self, name: str, arguments: Any) -> str:
        """Short, safe summary of validated arguments for progress events (AC-AGT-16): schema keys only, strings
        whitespace-collapsed and cut to 40 characters, at most 120 characters overall; "invalid arguments" otherwise."""
        parsed = self._parse(name, arguments)
        if isinstance(parsed, ToolResult):
            return "invalid arguments"

        def fmt(value: Any) -> str:
            if isinstance(value, dict):
                return "{" + ", ".join(f"{k}={fmt(v)}" for k, v in value.items()) + "}"
            if isinstance(value, list):
                return "[" + ", ".join(fmt(v) for v in value) + "]"
            if isinstance(value, str):
                text = " ".join(value.split())
                return json.dumps(text[:40] + ("..." if len(text) > 40 else ""))
            return json.dumps(value, default=str)

        dumped = parsed.model_dump(mode="json", exclude_none=True)
        summary = ", ".join(f"{k}={fmt(v)}" for k, v in dumped.items())
        return summary if len(summary) <= 120 else summary[:117] + "..."

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
    async def locations(self) -> dict[str, _Location]:
        """{location_id: _Location} from GET /locations, cached for ``LOCATIONS_TTL_S`` seconds."""
        now = self.clock()
        if self._locations is None or now - self._locations[0] > LOCATIONS_TTL_S:
            rows = await self.inventory.locations()
            locs = {
                str(r["location_id"]): _Location(
                    location_id=str(r["location_id"]),
                    parent_id=str(r["parent_id"]) if r.get("parent_id") else None,
                    kind=str(r.get("kind") or ""),
                    display_name=_cap(r.get("display_name"), MAX_CATALOG_TEXT) or "",
                )
                for r in rows
                if isinstance(r, dict) and r.get("location_id")
            }
            self._locations = (now, locs)
        return self._locations[1]

    async def location_names(self) -> dict[str, str | None]:
        """{location_id: display_name} from GET /locations (cached)."""
        return {k: (v.display_name or None) for k, v in (await self.locations()).items()}

    async def resolve_location(self, value: str) -> str | None:
        """A location_id for ``value``: the id itself (any case), an exact display name, or a module alias such as
        "Node 1", "Unity" or "Destiny" (AC-AGT-14). None when nothing matches."""
        locs = await self.locations()
        wanted = value.strip().lower()
        for lid in locs:
            if lid.lower() == wanted:
                return lid
        for loc in locs.values():
            if loc.display_name and loc.display_name.lower() == wanted:
                return loc.location_id
        for loc in locs.values():
            if loc.kind == "module" and loc.display_name and wanted in {a.lower() for a in module_aliases(loc)}:
                return loc.location_id
        return None

    async def _location_or_raw(self, value: str) -> str:
        """Resolved location_id, or ``value`` unchanged when unknown or GET /locations is unavailable."""
        try:
            return await self.resolve_location(value) or value
        except InventoryError:
            return value

    async def _subtree(self, location_id: str) -> frozenset[str]:
        locs = await self.locations()
        out = {location_id}
        for lid, loc in locs.items():
            seen, cur = set(), loc
            while cur is not None and cur.location_id not in seen:
                if cur.location_id == location_id:
                    out.add(lid)
                    break
                seen.add(cur.location_id)
                cur = locs.get(cur.parent_id) if cur.parent_id else None
        return frozenset(out)

    async def _events(self, params: dict[str, Any], wanted: list[str]) -> list[dict[str, Any]]:
        """GET /events for the wanted event types: ``type`` for one type, a ``types=`` comma list for several; an API
        without ``types`` (422) is asked again without it. Always filtered client-side as well."""
        p = dict(params)
        if len(wanted) == 1:
            p["type"] = wanted[0]
        elif wanted:
            p["types"] = ",".join(wanted)
        try:
            rows = await self.inventory.list_events(p)
        except InventoryError as exc:
            if exc.status_code != 422 or "types" not in p:
                raise
            p.pop("types")
            rows = await self.inventory.list_events(p)
        keep = set(wanted)
        return [e for e in rows or [] if isinstance(e, dict) and e.get("event_type") in keep]

    async def _labels(self, entity_ids: list[str]) -> dict[str, dict[str, Any]]:
        """GET /items/{id} for at most ``MAX_LABEL_LOOKUPS`` distinct entities, concurrently (errors -> skipped)."""
        ids = list(dict.fromkeys(entity_ids))[:MAX_LABEL_LOOKUPS]

        async def one(eid: str) -> tuple[str, dict[str, Any] | None]:
            try:
                return eid, await self.inventory.get_item(eid)
            except InventoryError:
                return eid, None

        return {eid: item for eid, item in await asyncio.gather(*(one(e) for e in ids)) if isinstance(item, dict)}

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
        return data, [_cite_item(i) for i in data]

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
        return data, [_cite_item(item)]

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
        by_id = {i.get("entity_id"): i for i in items}
        return data, [_cite_item(by_id.get(i["entity_id"], i)) for i in data]

    async def _who_moved(self, a: WhoMovedArgs):
        items = await self._resolve(a.name, limit=MAX_HANDLER_ENTITIES)
        wanted = [t for t in HANDLING_EVENT_TYPES if t in set(a.event_types or HANDLING_EVENT_TYPES)]

        def params(item: dict[str, Any]) -> dict[str, Any]:
            p: dict[str, Any] = {"entity_id": item["entity_id"], "limit": 50}
            if a.since is not None:
                p["after"] = _iso(a.since)
            return p

        per_item = await asyncio.gather(*(self._events(params(i), wanted) for i in items))
        fields = ("event_id", "event_type", "actor_desc", "from_location", "to_location", "occurred_at", "confidence",
                  "source")
        flat = [
            (item, {k: (clean_text(e.get(k)) if k == "actor_desc" else e.get(k)) for k in fields})
            for item, events in zip(items, per_item, strict=True)
            for e in events
        ]
        flat.sort(key=lambda pair: _parse_ts(pair[1].get("occurred_at")) or datetime.min.replace(tzinfo=UTC),
                  reverse=True)
        flat = flat[:MAX_HANDLER_EVENTS]
        data: list[dict[str, Any]] = []
        index: dict[str, dict[str, Any]] = {}
        citations = []
        for item, move in flat:  # newest first: entities ordered by their newest event
            entry = index.get(item["entity_id"])
            if entry is None:
                entry = {"entity_id": item["entity_id"], "label": item.get("label"),
                         "last_location": item.get("last_location"), "moves": []}
                index[item["entity_id"]] = entry
                data.append(entry)
            entry["moves"].append(move)
            where = move.get("to_location") or move.get("from_location") or item.get("last_location")
            citations.append(_cite_item(item, move.get("occurred_at"), location=where,
                                        location_name="" if where != item.get("last_location") else None))
        return data, citations

    async def _recent_changes(self, a: RecentChangesArgs):
        now = self.now()
        since = a.since or now - RECENT_DEFAULT_WINDOW
        if since > now + timedelta(minutes=1):
            raise _Invalid("since: must not be in the future")
        if since < now - RECENT_MAX_WINDOW:
            raise _Invalid("since: at most 7 days back")
        location = None
        if a.location:
            location = await self.resolve_location(a.location)
            if location is None:
                modules = sorted(k for k, v in (await self.locations()).items() if v.kind == "module")
                raise _Invalid(f"location: unknown location; use a location_id such as {', '.join(modules[:12])}")
        wanted = set(a.types or CHANGE_TYPES)
        ev_types = [t for t in HANDLING_EVENT_TYPES if t in wanted]
        inside = await self._subtree(location) if location else None
        names = await self.location_names()

        async def events() -> list[dict[str, Any]]:
            if not ev_types:
                return []
            return await self._events({"after": _iso_z(since), "limit": 200}, ev_types)

        async def new_items() -> list[dict[str, Any]]:
            if "new_item" not in wanted:
                return []
            params: dict[str, Any] = {"seen_after": _iso_z(since), "limit": 200}
            if location:
                params["location"] = location
            page = await self.inventory.list_items(params)
            return [i for i in _live(page.get("items", []))
                    if (_parse_ts(i.get("first_seen_at")) or since - timedelta(seconds=1)) >= since]

        async def resolved() -> list[dict[str, Any]]:
            if "review_resolved" not in wanted:
                return []
            params: dict[str, Any] = {"status": "resolved", "limit": 100}
            if location:
                params["location"] = location
            rows = await self.inventory.list_review(params)
            return [r for r in rows or [] if isinstance(r, dict)
                    and (_parse_ts(r.get("resolved_at")) or since - timedelta(seconds=1)) >= since]

        evs, items, reviews = await asyncio.gather(events(), new_items(), resolved())
        if inside is not None:
            evs = [e for e in evs if e.get("from_location") in inside or e.get("to_location") in inside]

        def loc_name(lid: Any, given: Any = None) -> str | None:
            return clean_text(given) or (names.get(lid) if lid else None)

        changes: list[dict[str, Any]] = []
        for e in evs:
            changes.append({
                "change": e.get("event_type"), "event_id": e.get("event_id"), "entity_id": e.get("entity_id"),
                "label": clean_text(e.get("entity_label")),
                "from_location": e.get("from_location"),
                "from_location_name": loc_name(e.get("from_location"), e.get("from_location_name")),
                "to_location": e.get("to_location"),
                "to_location_name": loc_name(e.get("to_location"), e.get("to_location_name")),
                "actor": clean_text(e.get("actor_desc")), "at": e.get("occurred_at"), "source": e.get("source"),
            })
        added = {e.get("entity_id") for e in evs if e.get("event_type") == "added"}
        for i in items:
            if i.get("entity_id") in added:
                continue  # an operator add is already listed as its `added` event
            changes.append({
                "change": "new_item", "entity_id": i.get("entity_id"), "label": clean_text(i.get("label")),
                "status": i.get("status"), "location": i.get("last_location"),
                "location_name": loc_name(i.get("last_location"), i.get("last_location_name")),
                "at": i.get("first_seen_at"),
            })
        for r in reviews:
            subject = r.get("subject") if isinstance(r.get("subject"), dict) else {}
            resolution = r.get("resolution") if isinstance(r.get("resolution"), dict) else {}
            changes.append({
                "change": "review_resolved", "review_id": r.get("review_id"), "review_kind": r.get("kind"),
                "entity_id": r.get("entity_id"), "label": clean_text(subject.get("label")),
                "location": r.get("location_id"), "location_name": loc_name(r.get("location_id")),
                "action": clean_text(resolution.get("action"), 40), "actor": clean_text(r.get("resolved_by")),
                "at": r.get("resolved_at"),
            })
        changes.sort(key=lambda c: _parse_ts(c.get("at")) or datetime.min.replace(tzinfo=UTC), reverse=True)
        omitted = max(0, len(changes) - MAX_RECENT_CHANGES)
        changes = changes[:MAX_RECENT_CHANGES]
        missing = [c["entity_id"] for c in changes if c.get("entity_id") and not c.get("label")]
        if missing:
            found = await self._labels(missing)
            for c in changes:
                item = found.get(c.get("entity_id"))
                if item and not c.get("label"):
                    c["label"] = clean_text(item.get("label"))
        citations = []
        for c in changes:
            where = c.get("to_location") or c.get("from_location") or c.get("location")
            where_name = c.get("to_location_name") or c.get("from_location_name") or c.get("location_name")
            citations.append(_cite_entity(c.get("entity_id"), c.get("at"), label=c.get("label"), location=where,
                                          location_name=where_name))
        data: dict[str, Any] = {"since": _iso_z(since), "until": _iso_z(now), "location": location,
                                "changes": changes}
        if omitted:
            data["omitted"] = omitted
        return data, citations

    async def _list_unknowns(self, a: ListUnknownsArgs):
        params: dict[str, Any] = {"status": "unknown", "limit": 100}
        if a.location:
            params["location"] = await self._location_or_raw(a.location)
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
        return data, [_cite_item(i) for i in data]

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
            label = clean_text(e.get("display_name"), MAX_CITATION_TEXT)
            if label:
                cite["label"] = label
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
