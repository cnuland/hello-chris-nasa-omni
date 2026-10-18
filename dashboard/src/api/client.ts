// Thin fetch client over the /api (inventory-api) and /agent proxies. Both bases are relative: the Vite dev proxy
// and the nginx container route them to the configured upstreams, so no URLs or credentials live in the bundle.
import type {
  CatalogItem, ChatMessage, ChatStreamEvent, Event, Filters, Item, ItemDetail, ItemPage, ItemPatch, Location,
  ResolveRequest, ReviewFilters, ReviewItem, Stats, TimelineEntry,
} from './types';
import { filtersToItemsQuery } from '../lib/filters';
import { SSEParser } from '../lib/sse';
import { log } from '../lib/log';

export const API_BASE = '/api';
export const AGENT_BASE = '/agent';

export class ApiError extends Error {
  constructor(public status: number, message: string) {
    super(message);
    this.name = 'ApiError';
  }
}

/** Extracts a human message from an error response body (FastAPI `detail`, string or validation list). */
async function errorFromResponse(res: Response): Promise<ApiError> {
  let detail = '';
  try {
    const text = await res.text();
    try {
      const body = JSON.parse(text) as { detail?: unknown };
      if (typeof body.detail === 'string') detail = body.detail;
      else if (Array.isArray(body.detail)) {
        detail = body.detail
          .map((d) => (d && typeof d === 'object' && 'msg' in d ? String((d as { msg: unknown }).msg) : String(d)))
          .join('; ');
      } else if (body.detail !== undefined) detail = JSON.stringify(body.detail);
    } catch {
      detail = text.slice(0, 200);
    }
  } catch {
    /* body unreadable */
  }
  return new ApiError(res.status, detail || `HTTP ${res.status} ${res.statusText}`.trim());
}

/** fetch() wrapper: network failures and non-2xx responses become ApiError (status 0 = network). */
async function request(url: string, init?: RequestInit): Promise<Response> {
  let res: Response;
  try {
    res = await fetch(url, init);
  } catch (e) {
    if (e instanceof DOMException && e.name === 'AbortError') throw e;
    log.warn('network error', { url, error: String(e) });
    throw new ApiError(0, `network error (${e instanceof Error ? e.message : String(e)})`);
  }
  if (!res.ok) {
    const err = await errorFromResponse(res);
    log.warn('api error', { url, status: res.status, detail: err.message });
    throw err;
  }
  return res;
}

async function json<T>(url: string, init?: RequestInit): Promise<T> {
  const res = await request(url, init);
  if (res.status === 204) return undefined as T;
  return (await res.json()) as T;
}

const jsonBody = (method: string, body: unknown): RequestInit => ({
  method,
  headers: { 'content-type': 'application/json' },
  body: JSON.stringify(body),
});

const qs = (p: URLSearchParams) => {
  const s = p.toString();
  return s ? `?${s}` : '';
};

export const listItems = (f: Filters, signal?: AbortSignal): Promise<ItemPage> =>
  json(`${API_BASE}/items${qs(filtersToItemsQuery(f))}`, { signal });
export const getItem = (id: string): Promise<ItemDetail> => json(`${API_BASE}/items/${encodeURIComponent(id)}`);
export const getHistory = (id: string): Promise<TimelineEntry[]> =>
  json(`${API_BASE}/items/${encodeURIComponent(id)}/history`);
export const patchItem = (id: string, body: ItemPatch): Promise<Item> =>
  json(`${API_BASE}/items/${encodeURIComponent(id)}`, jsonBody('PATCH', body));
/** POST /items as multipart/form-data (the browser sets the boundary header). */
export const createItem = (form: FormData): Promise<Item> => json(`${API_BASE}/items`, { method: 'POST', body: form });
/** inventory-api caps GET /review at limit=500 (default 50). */
export const REVIEW_MAX_LIMIT = 500;
export const listReview = (status = 'open', limit = REVIEW_MAX_LIMIT): Promise<ReviewItem[]> =>
  json(`${API_BASE}/review${qs(new URLSearchParams({ status, limit: String(limit) }))}`);
export interface ReviewPage {
  items: ReviewItem[];
  /** X-Total-Count when the API sends it (AC-UI-16), else null. */
  total: number | null;
}
/** GET /review with the queue filters (kind, location, q, sort, order) and the total from X-Total-Count. */
export async function listReviewPage(f: ReviewFilters = {}, limit = REVIEW_MAX_LIMIT, signal?: AbortSignal): Promise<ReviewPage> {
  const p = new URLSearchParams({ status: 'open', limit: String(limit) });
  if (f.kind) p.set('kind', f.kind);
  if (f.location) p.set('location', f.location);
  if (f.q?.trim()) p.set('q', f.q.trim());
  if (f.sort) p.set('sort', f.sort);
  if (f.order) p.set('order', f.order);
  const res = await request(`${API_BASE}/review${qs(p)}`, { signal });
  const body = (await res.json()) as unknown;
  const header = res.headers.get('x-total-count');
  const total = header !== null && /^\d+$/.test(header.trim()) ? Number(header) : null;
  return { items: Array.isArray(body) ? (body as ReviewItem[]) : [], total };
}
export const resolveReview = (id: string, body: ResolveRequest): Promise<unknown> =>
  json(`${API_BASE}/review/${encodeURIComponent(id)}/resolve`, jsonBody('POST', body));
/** UI2-05: undo a merge. `id` is the merged source item; returns the restored items (409 when unsafe to undo). */
export const unmergeItem = (id: string, body: { operator: string; note?: string }): Promise<Item[] | unknown> =>
  json(`${API_BASE}/items/${encodeURIComponent(id)}/unmerge`, jsonBody('POST', body));
export const listCatalog = (): Promise<CatalogItem[]> => json(`${API_BASE}/catalog`);
export const listLocations = (): Promise<Location[]> => json(`${API_BASE}/locations`);
export const listEvents = (p: Record<string, string> = {}, signal?: AbortSignal): Promise<Event[]> =>
  json(`${API_BASE}/events${qs(new URLSearchParams(p))}`, { signal });
export const getStats = (signal?: AbortSignal): Promise<Stats> => json(`${API_BASE}/stats`, { signal });

/**
 * POST /agent/chat/stream and dispatch SSE events as they arrive. Resolves when the stream ends; HTTP/network
 * failures reject with ApiError. In-stream `error` events are delivered through onEvent.
 */
export async function streamChat(
  messages: ChatMessage[],
  onEvent: (e: ChatStreamEvent) => void,
  signal?: AbortSignal,
  sessionId?: string,
): Promise<void> {
  const res = await request(`${AGENT_BASE}/chat/stream`, {
    ...jsonBody('POST', sessionId ? { messages, session_id: sessionId } : { messages }),
    headers: { 'content-type': 'application/json', accept: 'text/event-stream' },
    signal,
  });
  const parser = new SSEParser();
  if (!res.body) {
    for (const e of parser.push(`${await res.text()}\n\n`)) onEvent(e);
    return;
  }
  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  for (;;) {
    const { value, done } = await reader.read();
    if (done) break;
    for (const e of parser.push(decoder.decode(value, { stream: true }))) onEvent(e);
  }
  for (const e of parser.push(`${decoder.decode()}\n\n`)) onEvent(e);
}
