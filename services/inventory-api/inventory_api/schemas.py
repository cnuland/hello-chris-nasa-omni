"""Request models (validated before any write). Mirrors contracts/openapi/inventory-api.yaml."""

from __future__ import annotations

import math
from datetime import datetime
from typing import Annotated, Any, Literal
from uuid import UUID

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, StrictFloat, StrictInt, model_validator

EMBEDDING_DIM = 768
HALFVEC_MAX = 65504.0  # largest finite IEEE half-precision value; pgvector halfvec rejects anything larger
CATALOG_ID_PATTERN = r"^[a-z0-9][a-z0-9-]{1,63}$"

EntityKind = Literal["object", "person"]
EntityStatus = Literal["confirmed", "provisional", "unknown", "merged", "dismissed"]
EditableStatus = Literal["confirmed", "provisional", "unknown", "dismissed"]
NewEntityStatus = Literal["confirmed", "provisional", "unknown"]
EventType = Literal["moved", "added", "removed", "used", "handed_over", "seen", "stowed", "unstowed", "consumed"]
EventSource = Literal["pipeline", "operator", "speech"]
ReviewKind = Literal["unknown_object", "possible_duplicate", "low_confidence", "conflict"]
ReviewStatus = Literal["open", "resolved", "dismissed"]
CatalogCategory = Literal["object", "person", "container"]
ResolveAction = Literal["assign_catalog", "new_catalog", "merge_into", "dismiss"]
ItemSort = Literal["last_seen_at", "-last_seen_at", "label", "-label", "confidence", "-confidence"]

Probability = Annotated[float, Field(ge=0.0, le=1.0, allow_inf_nan=False)]
NonEmptyStr = Annotated[str, Field(min_length=1)]


def _check_embedding(values: list[float]) -> list[float]:
    if any(not math.isfinite(v) for v in values):
        raise ValueError("embedding values must be finite")
    if any(abs(v) > HALFVEC_MAX for v in values):
        raise ValueError(f"embedding values must be within +/-{HALFVEC_MAX:g} (halfvec range)")
    if not any(v != 0.0 for v in values):
        raise ValueError("embedding must not be the zero vector")
    return values


Embedding = Annotated[
    list[StrictFloat | StrictInt],
    Field(min_length=EMBEDDING_DIM, max_length=EMBEDDING_DIM),
    AfterValidator(_check_embedding),
]


class _Model(BaseModel):
    """Base: unknown extra fields are ignored (forward compatible), values are validated strictly enough."""

    model_config = ConfigDict(extra="ignore")


# ------------------------------------------------------------------ catalog
class CatalogItemIn(_Model):
    catalog_id: Annotated[str, Field(pattern=CATALOG_ID_PATTERN)]
    class_name: NonEmptyStr
    display_name: NonEmptyStr
    synonyms: list[str] = Field(default_factory=list)
    category: CatalogCategory = "object"
    is_fungible: bool = False
    expected_count: Annotated[int, Field(ge=0)] | None = None
    home_location: str | None = None
    # Not in the CatalogItem contract; accepted so POST /catalog can attribute the write (AC-API-21).
    operator: str | None = None


# ------------------------------------------------------------------ items
class ItemPatch(_Model):
    """PATCH body. Only fields present in the request are applied (see ``model_fields_set``)."""

    label: NonEmptyStr | None = None
    description: str | None = None
    catalog_id: str | None = None
    last_location: NonEmptyStr | None = None
    status: EditableStatus | None = None
    attributes: dict[str, Any] | None = None
    needs_review: bool | None = None
    operator: str | None = None
    note: str | None = None

    @model_validator(mode="after")
    def _non_nullable(self) -> ItemPatch:
        for name in ("label", "last_location", "status", "attributes", "needs_review"):
            if name in self.model_fields_set and getattr(self, name) is None:
                raise ValueError(f"{name} cannot be null")
        return self


class MergeRequest(_Model):
    into_entity_id: UUID
    operator: str | None = None
    note: str | None = None


# ------------------------------------------------------------------ review
class ResolveRequest(_Model):
    action: ResolveAction
    operator: NonEmptyStr
    catalog_id: str | None = None
    new_catalog: CatalogItemIn | None = None
    target_entity_id: UUID | None = None
    label: NonEmptyStr | None = None
    note: str | None = None

    @model_validator(mode="after")
    def _payload_for_action(self) -> ResolveRequest:
        if self.action == "assign_catalog" and not self.catalog_id:
            raise ValueError("assign_catalog requires catalog_id")
        if self.action == "new_catalog" and self.new_catalog is None:
            raise ValueError("new_catalog requires new_catalog")
        if self.action == "merge_into" and self.target_entity_id is None:
            raise ValueError("merge_into requires target_entity_id")
        return self


# ------------------------------------------------------------------ ingest
class CandidatesRequest(_Model):
    embedding: Embedding
    catalog_id: str | None = None
    location_id: str | None = None
    k: Annotated[int, Field(ge=1, le=200)] = 10


class WindowIn(_Model):
    camera_id: NonEmptyStr
    t_start: datetime
    t_end: datetime
    scene_summary: str | None = None
    transcript: str | None = None
    audio_events: list[Any] = Field(default_factory=list)
    model: NonEmptyStr
    raw_uri: str | None = None

    @model_validator(mode="after")
    def _ordered(self) -> WindowIn:
        if self.t_end < self.t_start:
            raise ValueError("t_end must be >= t_start")
        return self


class NewEntityIn(_Model):
    catalog_id: str | None = None
    kind: EntityKind = "object"
    status: NewEntityStatus = "provisional"
    label: NonEmptyStr
    description: str | None = None
    attributes: dict[str, Any] = Field(default_factory=dict)


class SightingIn(_Model):
    location_id: str | None = None
    seen_at: datetime
    bbox: Annotated[list[Annotated[float, Field(allow_inf_nan=False)]], Field(min_length=4, max_length=4)] | None = None
    confidence: Probability
    frame_uri: str | None = None
    description: str | None = None


class ExemplarIn(_Model):
    embedding: Embedding
    quality: Probability = 0.5
    crop_uri: str | None = None


class AssignmentIn(_Model):
    entity_id: UUID | None = None
    new_entity: NewEntityIn | None = None
    sighting: SightingIn
    exemplar: ExemplarIn | None = None
    ref: NonEmptyStr | None = None

    @model_validator(mode="after")
    def _entity_xor_new(self) -> AssignmentIn:
        if (self.entity_id is None) == (self.new_entity is None):
            raise ValueError("exactly one of entity_id or new_entity is required")
        return self


class _EntityRefMixin(_Model):
    entity_ref: NonEmptyStr | None = None
    entity_id: UUID | None = None


class EventIn(_EntityRefMixin):
    event_type: EventType
    actor_desc: str | None = None
    from_location: str | None = None
    to_location: str | None = None
    occurred_at: datetime
    confidence: Probability
    evidence_uri: str | None = None
    source: EventSource = "pipeline"

    @model_validator(mode="after")
    def _one_target(self) -> EventIn:
        if (self.entity_ref is None) == (self.entity_id is None):
            raise ValueError("an event needs exactly one of entity_ref or entity_id")
        return self


class ReviewIn(_EntityRefMixin):
    kind: ReviewKind
    candidate_ids: list[UUID] = Field(default_factory=list)
    image_uri: NonEmptyStr
    description: NonEmptyStr
    observed_at: datetime
    location_id: str | None = None
    model_guess: str | None = None
    probabilities: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _at_most_one_target(self) -> ReviewIn:
        if self.entity_ref is not None and self.entity_id is not None:
            raise ValueError("a review takes entity_ref or entity_id, not both")
        return self


class DecisionIn(_Model):
    question: NonEmptyStr
    subject: dict[str, Any]
    choice: NonEmptyStr
    probabilities: dict[str, Any] = Field(default_factory=dict)
    model: NonEmptyStr
    latency_ms: Annotated[int, Field(ge=0)] | None = None


class StockUpdateIn(_Model):
    catalog_id: NonEmptyStr
    location_id: NonEmptyStr
    est_count: Annotated[int, Field(ge=0)]
    seen_at: datetime


class IngestWindow(_Model):
    window: WindowIn
    assignments: list[AssignmentIn]
    events: list[EventIn] = Field(default_factory=list)
    reviews: list[ReviewIn] = Field(default_factory=list)
    decisions: list[DecisionIn] = Field(default_factory=list)
    stock_updates: list[StockUpdateIn] = Field(default_factory=list)

    @model_validator(mode="after")
    def _refs(self) -> IngestWindow:
        refs = [a.ref for a in self.assignments if a.ref is not None]
        if len(refs) != len(set(refs)):
            raise ValueError("duplicate assignment ref")
        known = set(refs)
        for i, ev in enumerate(self.events):
            if ev.entity_ref is not None and ev.entity_ref not in known:
                raise ValueError(f"events[{i}].entity_ref {ev.entity_ref!r} is not an assignment ref")
        for i, rv in enumerate(self.reviews):
            if rv.entity_ref is not None and rv.entity_ref not in known:
                raise ValueError(f"reviews[{i}].entity_ref {rv.entity_ref!r} is not an assignment ref")
        return self
