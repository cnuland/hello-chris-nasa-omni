// AC-UI-1..3: inventory table with URL-backed filters, sorting and pagination.
import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { Link, useSearchParams } from 'react-router-dom';
import { listItems } from '../api/client';
import type { Filters, Item, ItemPage, ItemStatus, Kind, SortKey } from '../api/types';
import { ConfidenceBar } from '../components/ConfidenceBar';
import { LocationOptions } from '../components/LocationSelect';
import { Thumb } from '../components/Thumb';
import {
  DEFAULT_SORT, KINDS, PAGE_SIZE, SEARCH_DEBOUNCE_MS, SORT_KEYS, STATUSES, filtersFromSearch, filtersToItemsQuery,
  filtersToSearch,
} from '../lib/filters';
import { Timestamp } from '../components/Timestamp';
import { LastHandledCell } from '../components/LastHandled';
import { isoToLocalInput, localInputToIso } from '../lib/format';
import { useLocations } from '../state/reference';
import { useToasts } from '../state/toasts';

const SORT_LABELS: Record<SortKey, string> = {
  '-last_seen_at': 'Last seen (newest first)',
  last_seen_at: 'Last seen (oldest first)',
  label: 'Label (A-Z)',
  '-label': 'Label (Z-A)',
  '-confidence': 'Confidence (high-low)',
  confidence: 'Confidence (low-high)',
};

const DASH = '—';
const QUICK_RANGES = [
  { label: 'Last 1 h', ms: 3600_000 },
  { label: 'Last 24 h', ms: 24 * 3600_000 },
];

function ItemRow({ item }: { item: Item }) {
  return (
    <tr data-testid={`item-${item.entity_id}`}>
      <td className="cell-thumb">
        <Thumb src={item.snapshot_url} alt={item.label} />
      </td>
      <td>
        <Link className="item-link" to={`/items/${item.entity_id}`}>{item.label}</Link>
        <div className="muted">{item.catalog_name || 'Uncatalogued'}</div>
      </td>
      <td>
        <span className={`status status-${item.status}`}>{item.status}</span>
        {item.needs_review && <span className="flag" title={item.review_reason ?? undefined}>Needs review</span>}
      </td>
      <td>
        <ConfidenceBar value={item.confidence} />
      </td>
      <td>
        {item.last_location ? (
          <>
            <div>{item.last_location_name || item.last_location}</div>
            <code className="loc-code">{item.last_location}</code>
          </>
        ) : (
          <span className="muted">{DASH}</span>
        )}
      </td>
      <td>
        <Timestamp iso={item.last_seen_at} stacked />
      </td>
      <td><LastHandledCell item={item} /></td>
    </tr>
  );
}

/** "YYYY-MM-DD HH:MM" (or with a T) typed as UTC -> ISO, undefined while incomplete/invalid. */
function typedUtcToIso(v: string): string | undefined {
  const m = /^(\d{4}-\d{2}-\d{2})[ T](\d{2}:\d{2})$/.exec(v.trim());
  return m ? localInputToIso(`${m[1]}T${m[2]}`) : undefined;
}

/** AC-UI-23 (UI-15): 24-hour UTC text entry instead of the locale datetime picker; commits once complete or cleared. */
function UtcInput({ id, label, value, onCommit }: { id: string; label: string; value?: string; onCommit: (iso?: string) => void }) {
  const shown = isoToLocalInput(value).replace('T', ' ');
  const [text, setText] = useState(shown);
  useEffect(() => setText(shown), [shown]);
  const invalid = text.trim() !== '' && !typedUtcToIso(text);
  return (
    <div className="field">
      <label htmlFor={id}>{label}</label>
      <input id={id} type="text" inputMode="numeric" placeholder="YYYY-MM-DD HH:MM" value={text} spellCheck={false}
        aria-invalid={invalid} aria-describedby={`${id}-hint`}
        onChange={(e) => {
          const v = e.target.value;
          setText(v);
          if (!v.trim()) onCommit(undefined);
          else {
            const iso = typedUtcToIso(v);
            if (iso) onCommit(iso);
          }
        }} />
      <span id={`${id}-hint`} className={invalid ? 'field-error' : 'muted small'}>
        {invalid ? 'Use YYYY-MM-DD HH:MM (24 h, UTC).' : '24 h, UTC'}
      </span>
    </div>
  );
}

export default function InventoryPage() {
  const [params, setParams] = useSearchParams();
  const search = params.toString();
  const filters = useMemo(() => filtersFromSearch(search), [search]);
  const queryKey = useMemo(() => filtersToItemsQuery(filters).toString(), [filters]);
  const { options: locationOptions } = useLocations();
  const toasts = useToasts();

  const [page, setPage] = useState<ItemPage | null>(null);
  const [loading, setLoading] = useState(true);
  const [failed, setFailed] = useState(false);

  /** Merge a filter change into the URL. Filter/sort changes reset paging unless an offset is given. */
  const update = useCallback(
    (patch: Partial<Filters>) => {
      setParams((prev) => {
        const next: Filters = { ...filtersFromSearch(prev), offset: undefined, ...patch };
        return filtersToSearch(next);
      });
    },
    [setParams],
  );

  // Fetch the page whenever the effective /items query changes.
  useEffect(() => {
    const ctrl = new AbortController();
    setLoading(true);
    listItems(filtersFromSearch(queryKey), ctrl.signal)
      .then((p) => {
        setPage(p);
        setFailed(false);
      })
      .catch((e) => {
        if (ctrl.signal.aborted) return;
        // Never keep showing rows/total from a different query than the filters on screen.
        setPage(null);
        setFailed(true);
        toasts.error('Could not load items', e);
      })
      .finally(() => {
        if (!ctrl.signal.aborted) setLoading(false);
      });
    return () => ctrl.abort();
  }, [queryKey, toasts]);

  // Debounced free-text search (300 ms after the last keystroke).
  const [text, setText] = useState(filters.q ?? '');
  const urlQ = filters.q ?? '';
  const textRef = useRef(text);
  textRef.current = text;
  useEffect(() => {
    if (textRef.current.trim() !== urlQ) setText(urlQ);
  }, [urlQ]);
  useEffect(() => {
    if (text.trim() === urlQ) return undefined;
    const t = setTimeout(() => update({ q: text.trim() || undefined }), SEARCH_DEBOUNCE_MS);
    return () => clearTimeout(t);
    // urlQ intentionally omitted: only keystrokes restart the debounce.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [text, update]);

  const toggleStatus = (s: ItemStatus, on: boolean) => {
    const cur = new Set(filters.status ?? []);
    if (on) cur.add(s);
    else cur.delete(s);
    update({ status: STATUSES.filter((x) => cur.has(x)) });
  };

  const hasFilters = filtersToSearch({ ...filters, sort: undefined, offset: undefined }).toString() !== '';
  const resetFilters = () => {
    setText('');
    setParams(filtersToSearch({ sort: filters.sort }));
  };

  const offset = filters.offset ?? 0;
  const total = page?.total ?? 0;
  const pages = Math.max(1, Math.ceil(total / PAGE_SIZE));
  const current = Math.min(pages, Math.floor(offset / PAGE_SIZE) + 1);
  const goTo = (newOffset: number) => update({ offset: newOffset > 0 ? newOffset : undefined });

  return (
    <div className="page inventory">
      <div className="page-head">
        <h1>Inventory</h1>
        <Link className="button primary" to="/items/new">Add item</Link>
      </div>

      <form className="filters" aria-label="Filters" onSubmit={(e) => e.preventDefault()}>
        <div className="field grow">
          <label htmlFor="f-q">Search</label>
          <input id="f-q" type="search" value={text} placeholder="label, description…" onChange={(e) => setText(e.target.value)} />
        </div>
        <div className="field">
          <label htmlFor="f-kind">Kind</label>
          <select id="f-kind" value={filters.kind ?? ''} onChange={(e) => update({ kind: (e.target.value || undefined) as Kind | undefined })}>
            <option value="">Any</option>
            {KINDS.map((k) => <option key={k} value={k}>{k}</option>)}
          </select>
        </div>
        <div className="field">
          <label htmlFor="f-loc">Location</label>
          <select id="f-loc" value={filters.location ?? ''} onChange={(e) => update({ location: e.target.value || undefined })}>
            <option value="">Anywhere</option>
            <LocationOptions options={locationOptions} />
          </select>
        </div>
        <div className="field">
          <label htmlFor="f-conf">Min confidence</label>
          <div className="range-row">
            <input
              id="f-conf" type="range" min={0} max={1} step={0.05} value={filters.min_confidence ?? 0}
              aria-valuetext={`${Math.round((filters.min_confidence ?? 0) * 100)}%`}
              onChange={(e) => update({ min_confidence: Number(e.target.value) || undefined })}
            />
            <span className="muted" aria-hidden="true">{`${Math.round((filters.min_confidence ?? 0) * 100)}%`}</span>
          </div>
        </div>
        <UtcInput id="f-after" label="Seen after (UTC)" value={filters.seen_after} onCommit={(iso) => update({ seen_after: iso })} />
        <UtcInput id="f-before" label="Seen before (UTC)" value={filters.seen_before} onCommit={(iso) => update({ seen_before: iso })} />
        <div className="field quick-ranges" role="group" aria-label="Quick time ranges">
          <span className="field-label" aria-hidden="true">Quick range</span>
          <div className="actions-inline">
            {QUICK_RANGES.map((r) => (
              <button key={r.label} type="button" onClick={() => update({ seen_after: new Date(Date.now() - r.ms).toISOString(), seen_before: undefined })}>
                {r.label}
              </button>
            ))}
          </div>
        </div>
        <div className="field sort-field">
          <label htmlFor="f-sort">Sort</label>
          <select id="f-sort" value={filters.sort ?? DEFAULT_SORT} onChange={(e) => update({ sort: e.target.value as SortKey })}>
            {SORT_KEYS.map((k) => <option key={k} value={k}>{SORT_LABELS[k]}</option>)}
          </select>
        </div>
        <div className="field reset-field">
          <span className="field-label" aria-hidden="true">{'\u00a0'}</span>
          <button type="button" disabled={!hasFilters} onClick={resetFilters}>Reset filters</button>
        </div>
        {/* UI2-17: "Needs review" belongs with the status filters, on one full-width row. */}
        <fieldset className="field status-group">
          <legend>Status</legend>
          {STATUSES.map((s) => (
            <label key={s} className="check">
              <input type="checkbox" checked={filters.status?.includes(s) ?? false} onChange={(e) => toggleStatus(s, e.target.checked)} />
              {s}
            </label>
          ))}
          <span className="status-sep" aria-hidden="true" />
          <label className="check needs-review-check">
            <input type="checkbox" checked={filters.needs_review ?? false} onChange={(e) => update({ needs_review: e.target.checked || undefined })} />
            Needs review
          </label>
        </fieldset>
      </form>

      <div className="list-meta" aria-live="polite">
        {page && <span className="count">{total === 0 ? 'No items' : `${total} ${total === 1 ? 'item' : 'items'}`}</span>}
        {loading && <span className="muted">Loading…</span>}
      </div>

      <div className="table-wrap">
        <table className="items">
          <thead>
            <tr>
              <th scope="col"><span className="sr-only">Snapshot</span></th>
              <th scope="col">Item</th>
              <th scope="col">Status</th>
              <th scope="col">Confidence</th>
              <th scope="col">Last location</th>
              <th scope="col">Last seen</th>
              <th scope="col">Last handled</th>
            </tr>
          </thead>
          <tbody>
            {page?.items.map((it) => <ItemRow key={it.entity_id} item={it} />)}
          </tbody>
        </table>
        {page && page.items.length === 0 && <p className="empty">Nothing matches the current filters.</p>}
        {!page && failed && <p className="empty">Inventory unavailable.</p>}
      </div>

      <div className="pager">
        <button type="button" disabled={offset <= 0} onClick={() => goTo(Math.max(0, offset - PAGE_SIZE))}>
          Previous
        </button>
        <span>{`Page ${current} of ${pages}`}</span>
        <button type="button" disabled={offset + PAGE_SIZE >= total} onClick={() => goTo(offset + PAGE_SIZE)}>
          Next
        </button>
      </div>
    </div>
  );
}
