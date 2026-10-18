// Types mirrored from contracts/openapi/inventory-api.yaml (authoritative) and the agent chat shape (AC-AGT-1/5).
export type Kind = 'object' | 'person';
export type ItemStatus = 'confirmed' | 'provisional' | 'unknown' | 'merged' | 'dismissed';
export type SortKey = 'last_seen_at' | '-last_seen_at' | 'label' | '-label' | 'confidence' | '-confidence';

export interface Item {
  entity_id: string;
  catalog_id?: string | null;
  catalog_name?: string | null;
  kind: Kind;
  status: ItemStatus;
  label: string;
  description?: string | null;
  attributes?: Record<string, unknown>;
  last_location?: string | null;
  last_location_name?: string | null;
  last_seen_at?: string | null;
  first_seen_at?: string | null;
  confidence: number;
  snapshot_url?: string | null;
  needs_review?: boolean;
  review_reason?: string | null;
  last_moved_by?: string | null;
  last_moved_at?: string | null;
  /** Latest moved/used/handed_over event (UI review AC-UI-12). Optional: older APIs omit it. */
  last_handled?: LastHandled | null;
  /** When / by whom last_location was last set (operator edit or pipeline). Operator edits no longer bump last_seen_at. */
  location_set_at?: string | null;
  location_set_by?: string | null;
}

export interface LastHandled {
  event_type?: string | null;
  actor_desc?: string | null;
  occurred_at?: string | null;
}

export interface ItemPage {
  items: Item[];
  total: number;
}

export interface Event {
  event_id?: string;
  event_type?: string;
  entity_id?: string;
  actor_desc?: string | null;
  from_location?: string | null;
  to_location?: string | null;
  occurred_at?: string;
  confidence?: number;
  evidence_url?: string | null;
  source?: string;
  /** Joined display fields (AC-UI-18); optional. */
  entity_label?: string | null;
  entity_catalog_id?: string | null;
  from_location_name?: string | null;
  to_location_name?: string | null;
}

export interface ItemDetail extends Item {
  sightings?: Record<string, unknown>[];
  events?: Event[];
  exemplar_count?: number;
}

export interface TimelineEntry {
  at: string;
  kind: 'sighting' | 'event' | 'audit';
  location_id?: string | null;
  summary?: string;
  image_url?: string | null;
  /** If the API collapses repeated rows itself, the number of rows this entry stands for (AC-UI-17). */
  count?: number;
  /** Collapsed entries only: time of the oldest folded audit. */
  first_at?: string | null;
  audit_ids?: string[];
}

export interface CatalogItem {
  catalog_id: string;
  class_name: string;
  display_name: string;
  synonyms?: string[];
  category?: 'object' | 'person' | 'container';
  is_fungible?: boolean;
  expected_count?: number | null;
  home_location?: string | null;
}

export interface Location {
  location_id: string;
  parent_id?: string | null;
  kind?: string;
  display_name: string;
}

/** Compact entity summary attached to review items (AC-UI-13). Every field is optional except entity_id. */
export interface ReviewEntity {
  entity_id: string;
  label?: string | null;
  catalog_id?: string | null;
  status?: string | null;
  last_location?: string | null;
  last_location_name?: string | null;
  last_seen_at?: string | null;
  confidence?: number | null;
  thumbnail_url?: string | null;
  /** Older payloads carry full Items (snapshot_url instead of thumbnail_url). */
  snapshot_url?: string | null;
  sighting_count?: number | null;
}

export interface ReviewCandidate extends ReviewEntity {
  similarity?: number | null;
}

/** Normalised [x1, y1, x2, y2] in 0..1 relative to image_url. */
export type BBox = [number, number, number, number];

export interface ReviewItem {
  review_id: string;
  kind: string;
  entity_id?: string | null;
  candidate_ids?: string[];
  candidates?: ReviewCandidate[];
  /** stored = candidate_ids expanded; suggested = server's nearest same-class items; none. */
  candidates_source?: 'stored' | 'suggested' | 'none' | null;
  subject?: ReviewEntity | null;
  image_bbox?: BBox | null;
  created_at?: string | null;
  image_url: string | null;
  description: string;
  observed_at: string;
  location_id?: string | null;
  model_guess?: string | null;
  probabilities?: Record<string, number>;
  status: string;
}

export type ResolveAction = 'assign_catalog' | 'new_catalog' | 'merge_into' | 'dismiss';
export interface ResolveRequest {
  action: ResolveAction;
  operator: string;
  catalog_id?: string;
  new_catalog?: CatalogItem;
  target_entity_id?: string;
  label?: string;
  note?: string;
}

export interface ItemPatch {
  label?: string;
  description?: string;
  catalog_id?: string | null;
  last_location?: string;
  status?: 'confirmed' | 'provisional' | 'unknown' | 'dismissed';
  attributes?: Record<string, unknown>;
  needs_review?: boolean;
  operator?: string;
  note?: string;
}

export interface Stats {
  by_status: Record<string, number>;
  by_kind: Record<string, number>;
  by_module: Record<string, number>;
  open_reviews_by_kind: Record<string, number>;
}

// ---- agent (services/agent) ----
export interface ChatMessage {
  role: 'user' | 'assistant';
  content: string;
}
/** Exactly one of entity_id | window_id | catalog_id (AC-AGT-11 adds catalog facts, which may have no `at`). */
export interface Citation {
  entity_id?: string;
  window_id?: string;
  catalog_id?: string;
  at?: string;
}
export interface Proposal {
  proposal_id?: string;
  entity_id: string;
  fields: ItemPatch;
  summary?: string;
  /** Current values snapshot, if the agent forwards it (propose_update tool result has it; harness may drop it). */
  current?: Record<string, unknown>;
}
export interface ChatResponse {
  reply: string;
  citations: Citation[];
  tool_calls?: unknown[];
  proposals?: Proposal[];
}
/** Events emitted by POST /agent/chat/stream (SSE): `event: token` {text}, `event: done` ChatResponse, `event: error` {detail}. */
export type ChatStreamEvent =
  | { type: 'token'; text: string }
  /** Progress while the agent works (AC-UI-21): optional free text and/or the tool being called. */
  | { type: 'status'; text?: string; tool?: string }
  | { type: 'done'; response: ChatResponse }
  | { type: 'error'; detail: string };

/** Review queue filters (AC-UI-16). Keys map 1:1 to GET /review params. */
export interface ReviewFilters {
  kind?: string;
  location?: string;
  q?: string;
  sort?: 'observed_at' | 'created_at';
  order?: 'asc' | 'desc';
}

/** Inventory filter state (AC-UI-2/3). Keys map 1:1 to GET /items query params. */
export interface Filters {
  q?: string;
  kind?: Kind;
  status?: ItemStatus[];
  location?: string;
  min_confidence?: number;
  seen_after?: string;
  seen_before?: string;
  needs_review?: boolean;
  sort?: SortKey;
  limit?: number;
  offset?: number;
}
