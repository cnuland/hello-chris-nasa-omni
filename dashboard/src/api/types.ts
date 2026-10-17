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

export interface ReviewItem {
  review_id: string;
  kind: string;
  entity_id?: string | null;
  candidate_ids?: string[];
  candidates?: Item[];
  image_url: string;
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
  | { type: 'done'; response: ChatResponse }
  | { type: 'error'; detail: string };

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
