// Validation of untrusted agent output (conventions: model outputs are validated before acting). The `done` payload of
// the chat stream is checked here; proposals are reduced to the PATCH /items allow-list before they can be confirmed.
import type { ChatResponse, Citation, ItemPatch, Proposal } from '../api/types';

export const UUID_RE = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
const PATCH_STATUSES = new Set(['confirmed', 'provisional', 'unknown', 'dismissed']);
const MAX_STR = 2000;
/** CatalogItem.catalog_id pattern (contracts/openapi/inventory-api.yaml). */
const CATALOG_ID_RE = /^[a-z0-9][a-z0-9-]{1,63}$/;

const isObj = (v: unknown): v is Record<string, unknown> => typeof v === 'object' && v !== null && !Array.isArray(v);
const isUuid = (v: unknown): v is string => typeof v === 'string' && UUID_RE.test(v);
const shortStr = (v: unknown, max = MAX_STR): v is string => typeof v === 'string' && v.length <= max;

/**
 * Reduces model-proposed fields to the PATCH /items allow-list with type checks (ProposalFields in the agent).
 * operator/note and unknown keys are dropped: they are set by the confirming human, never by the model.
 */
export function sanitizeProposalFields(raw: unknown): ItemPatch {
  const out: ItemPatch = {};
  if (!isObj(raw)) return out;
  if (shortStr(raw.label, 200) && raw.label.trim()) out.label = raw.label;
  if (shortStr(raw.description)) out.description = raw.description;
  if (raw.catalog_id === null || (shortStr(raw.catalog_id, 64) && raw.catalog_id.trim())) out.catalog_id = raw.catalog_id;
  if (shortStr(raw.last_location, 64) && raw.last_location.trim()) out.last_location = raw.last_location;
  if (typeof raw.status === 'string' && PATCH_STATUSES.has(raw.status)) out.status = raw.status as ItemPatch['status'];
  if (isObj(raw.attributes)) out.attributes = raw.attributes;
  if (typeof raw.needs_review === 'boolean') out.needs_review = raw.needs_review;
  return out;
}

/** PATCH body for confirming a proposal: allow-listed fields + the human operator + an audit note. */
export function proposalPatch(p: Proposal, operator: string): ItemPatch {
  const note = p.proposal_id ? `via agent proposal ${p.proposal_id}` : 'via agent proposal';
  return { ...sanitizeProposalFields(p.fields), operator: operator.trim(), note };
}

/** AC-UI-24: optional display fields from the agent (AC-AGT-17); plain strings up to 200 chars, else dropped. */
function citationLabels(raw: Record<string, unknown>): Pick<Citation, 'label' | 'location' | 'location_name'> {
  const out: Pick<Citation, 'label' | 'location' | 'location_name'> = {};
  for (const k of ['label', 'location', 'location_name'] as const) {
    const v = raw[k];
    if (typeof v === 'string' && v.trim() && v.length <= 200) out[k] = v;
  }
  return out;
}

function sanitizeCitation(raw: unknown): Citation | null {
  if (!isObj(raw)) return null;
  const at = typeof raw.at === 'string' ? raw.at : undefined;
  const labels = citationLabels(raw);
  const catalog = typeof raw.catalog_id === 'string' && CATALOG_ID_RE.test(raw.catalog_id) ? raw.catalog_id : undefined;
  if (isUuid(raw.entity_id)) return { entity_id: raw.entity_id, at, ...labels, ...(catalog ? { catalog_id: catalog } : {}) };
  if (shortStr(raw.window_id, 128) && raw.window_id) return { window_id: raw.window_id, at, ...labels };
  if (catalog) return { catalog_id: catalog, at, ...labels };
  return null;
}

function sanitizeProposal(raw: unknown): Proposal | null {
  if (!isObj(raw) || !isUuid(raw.entity_id) || !isObj(raw.fields)) return null;
  const fields = sanitizeProposalFields(raw.fields);
  if (Object.keys(fields).length === 0) return null;
  const p: Proposal = { entity_id: raw.entity_id, fields };
  if (shortStr(raw.proposal_id, 64) && raw.proposal_id) p.proposal_id = raw.proposal_id;
  if (shortStr(raw.summary, 500) && raw.summary) p.summary = raw.summary;
  if (isObj(raw.current)) p.current = raw.current;
  return p;
}

/**
 * Validates a chat `done` payload. Structural problems (not an object, non-string reply, non-array citations or
 * proposals) return an error string; individual malformed citations/proposals are dropped.
 */
export function validateChatResponse(raw: unknown): ChatResponse | string {
  if (!isObj(raw)) return 'malformed done event from the agent (not an object)';
  if (raw.reply !== undefined && typeof raw.reply !== 'string') return 'malformed done event from the agent (reply)';
  if (raw.citations !== undefined && raw.citations !== null && !Array.isArray(raw.citations)) {
    return 'malformed done event from the agent (citations)';
  }
  if (raw.proposals !== undefined && raw.proposals !== null && !Array.isArray(raw.proposals)) {
    return 'malformed done event from the agent (proposals)';
  }
  const citations = ((raw.citations as unknown[] | null) ?? []).map(sanitizeCitation).filter((c): c is Citation => !!c);
  const proposals = ((raw.proposals as unknown[] | null) ?? []).map(sanitizeProposal).filter((p): p is Proposal => !!p);
  const out: ChatResponse = { reply: (raw.reply as string | undefined) ?? '', citations };
  if (Array.isArray(raw.tool_calls)) out.tool_calls = raw.tool_calls;
  if (proposals.length || raw.proposals !== undefined) out.proposals = proposals;
  return out;
}
