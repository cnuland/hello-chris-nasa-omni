// AC-UI-2 / AC-UI-3: URL query string <-> filter state <-> GET /items params (1:1).
import type { Filters, ItemStatus, Kind, SortKey } from '../api/types';

export const PAGE_SIZE = 50;
export const DEFAULT_SORT: SortKey = '-last_seen_at';
export const SEARCH_DEBOUNCE_MS = 300;

export const KINDS: readonly Kind[] = ['object', 'person'];
export const STATUSES: readonly ItemStatus[] = ['confirmed', 'provisional', 'unknown', 'merged', 'dismissed'];
export const SORT_KEYS: readonly SortKey[] = [
  '-last_seen_at', 'last_seen_at', 'label', '-label', '-confidence', 'confidence',
];

const isKind = (v: string): v is Kind => (KINDS as readonly string[]).includes(v);
const isStatus = (v: string): v is ItemStatus => (STATUSES as readonly string[]).includes(v);
const isSort = (v: string): v is SortKey => (SORT_KEYS as readonly string[]).includes(v);

const HAS_OFFSET = /(Z|[+-]\d{2}:?\d{2})$/i;
const DATE_TIME = /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(:\d{2}(\.\d+)?)?$/;

/**
 * Normalise a date-time string to ISO-8601 UTC, or undefined if it does not parse. Offset-less date-times (e.g. a
 * hand-written shared URL `seen_after=2026-10-17T10:00`) are read as UTC, matching the "(UTC)" filter inputs, rather
 * than in the browser's local time zone.
 */
function isoOrUndefined(v: string | null): string | undefined {
  if (!v) return undefined;
  const s = v.trim();
  const t = Date.parse(DATE_TIME.test(s) && !HAS_OFFSET.test(s) ? `${s}Z` : s);
  return Number.isNaN(t) ? undefined : new Date(t).toISOString();
}

/** Parse the browser query string into filter state (unknown keys ignored, invalid values dropped). */
export function filtersFromSearch(search: URLSearchParams | string): Filters {
  const p = typeof search === 'string' ? new URLSearchParams(search.replace(/^\?/, '')) : search;
  const f: Filters = {};

  const q = p.get('q')?.trim();
  if (q) f.q = q;

  const kind = p.get('kind');
  if (kind && isKind(kind)) f.kind = kind;

  const status = [...new Set(p.getAll('status').filter(isStatus))];
  if (status.length) f.status = status;

  const location = p.get('location')?.trim();
  if (location) f.location = location;

  const mc = p.get('min_confidence');
  if (mc !== null && mc.trim() !== '') {
    const n = Number(mc);
    if (Number.isFinite(n) && n >= 0 && n <= 1) f.min_confidence = n;
  }

  const after = isoOrUndefined(p.get('seen_after'));
  if (after) f.seen_after = after;
  const before = isoOrUndefined(p.get('seen_before'));
  if (before) f.seen_before = before;

  if (p.get('needs_review') === 'true') f.needs_review = true;

  const sort = p.get('sort');
  if (sort && isSort(sort)) f.sort = sort;

  const off = p.get('offset');
  if (off !== null && /^\d+$/.test(off)) {
    const n = Number(off);
    if (n > 0) f.offset = n;
  }
  return f;
}

/** Appends the filter (not paging/sort) params shared by the URL and the API query. */
function appendFilterParams(s: URLSearchParams, f: Filters): void {
  if (f.q?.trim()) s.set('q', f.q.trim());
  if (f.kind) s.set('kind', f.kind);
  for (const st of f.status ?? []) s.append('status', st);
  if (f.location) s.set('location', f.location);
  if (f.min_confidence !== undefined && f.min_confidence > 0) s.set('min_confidence', String(f.min_confidence));
  if (f.seen_after) s.set('seen_after', f.seen_after);
  if (f.seen_before) s.set('seen_before', f.seen_before);
  if (f.needs_review) s.set('needs_review', 'true');
}

/** Serialise filter state to a shareable query string (empty values omitted, status repeated). */
export function filtersToSearch(filters: Filters): URLSearchParams {
  const s = new URLSearchParams();
  appendFilterParams(s, filters);
  if (filters.sort && filters.sort !== DEFAULT_SORT) s.set('sort', filters.sort);
  if (filters.offset && filters.offset > 0) s.set('offset', String(filters.offset));
  return s;
}

/** Build the exact GET /items query (always includes sort, limit=50 and offset). */
export function filtersToItemsQuery(filters: Filters): URLSearchParams {
  const s = new URLSearchParams();
  appendFilterParams(s, filters);
  s.set('sort', filters.sort ?? DEFAULT_SORT);
  s.set('limit', String(filters.limit ?? PAGE_SIZE));
  s.set('offset', String(filters.offset ?? 0));
  return s;
}
