// AC-UI-7 / AC-UI-13..16: review queue (unknowns, possible duplicates, low confidence, conflicts) with classify / new
// catalog item / merge / keep both / dismiss actions, queue filters and sort. Resolved cards disappear and the nav badge
// is refreshed.
import { useEffect, useMemo, useRef, useState } from 'react';
import { Link, useSearchParams } from 'react-router-dom';
import { ApiError, listReviewPage, resolveReview } from '../api/client';
import type { CatalogItem, Location, ResolveRequest, ReviewCandidate, ReviewEntity, ReviewFilters, ReviewItem } from '../api/types';
import { ConfidenceBar } from '../components/ConfidenceBar';
import { FrameImage } from '../components/FrameImage';
import { LocationOptions } from '../components/LocationSelect';
import { Timestamp } from '../components/Timestamp';
import { SEARCH_DEBOUNCE_MS } from '../lib/filters';
import { humanize, locationLabel, shortId } from '../lib/format';
import { useCachedItem } from '../state/itemCache';
import { useCatalog, useLocations } from '../state/reference';
import { useOperator } from '../state/operator';
import { useReviewCount } from '../state/reviewCount';
import { useToasts } from '../state/toasts';

export const CATALOG_ID_PATTERN = /^[a-z0-9][a-z0-9-]{1,63}$/;

type Resolve = (body: Omit<ResolveRequest, 'operator'>) => Promise<void>;

export const REVIEW_KINDS: { kind: string; label: string }[] = [
  { kind: 'unknown_object', label: 'Unknown objects' },
  { kind: 'possible_duplicate', label: 'Possible duplicates' },
  { kind: 'low_confidence', label: 'Low confidence' },
  { kind: 'conflict', label: 'Conflicts' },
];

const SORTS: { value: string; label: string; sort: ReviewFilters['sort']; order: ReviewFilters['order'] }[] = [
  { value: 'created_at:desc', label: 'Newest in queue first', sort: 'created_at', order: 'desc' },
  { value: 'created_at:asc', label: 'Oldest in queue first', sort: 'created_at', order: 'asc' },
  { value: 'observed_at:desc', label: 'Most recently observed first', sort: 'observed_at', order: 'desc' },
  { value: 'observed_at:asc', label: 'Least recently observed first', sort: 'observed_at', order: 'asc' },
];

function catalogMatches(c: CatalogItem, q: string): boolean {
  const needle = q.trim().toLowerCase();
  if (!needle) return true;
  return [c.display_name, c.class_name, c.catalog_id, ...(c.synonyms ?? [])].some((s) => s?.toLowerCase().includes(needle));
}

// ---------------------------------------------------------------- filters (URL <-> state, client-side fallback)
export function reviewFiltersFromSearch(p: URLSearchParams): ReviewFilters {
  const f: ReviewFilters = {};
  const kind = p.get('kind');
  if (kind && REVIEW_KINDS.some((k) => k.kind === kind)) f.kind = kind;
  const loc = p.get('location')?.trim();
  if (loc) f.location = loc;
  const q = p.get('q')?.trim();
  if (q) f.q = q.slice(0, 200);
  const sort = p.get('sort');
  if (sort === 'observed_at' || sort === 'created_at') f.sort = sort;
  const order = p.get('order');
  if (order === 'asc' || order === 'desc') f.order = order;
  return f;
}

function reviewFiltersToSearch(f: ReviewFilters): URLSearchParams {
  const s = new URLSearchParams();
  if (f.kind) s.set('kind', f.kind);
  if (f.location) s.set('location', f.location);
  if (f.q?.trim()) s.set('q', f.q.trim());
  if (f.sort) s.set('sort', f.sort);
  if (f.order) s.set('order', f.order);
  return s;
}

function isWithin(id: string | null | undefined, root: string, parents: Map<string, string | null>): boolean {
  let cur: string | null | undefined = id;
  for (let i = 0; cur && i < 32; i++) {
    if (cur === root) return true;
    cur = parents.get(cur) ?? null;
  }
  return false;
}

/**
 * Applies the queue filters client-side. Used only when the API did not send X-Total-Count (an older inventory-api that
 * ignores kind/location/q/sort), so results match what a filtering API would return.
 */
export function applyReviewFilters(items: ReviewItem[], f: ReviewFilters, locations: Location[]): ReviewItem[] {
  const parents = new Map(locations.map((l) => [l.location_id, l.parent_id ?? null]));
  const needle = f.q?.trim().toLowerCase();
  let out = items.filter((r) => {
    if (f.kind && r.kind !== f.kind) return false;
    if (f.location && !isWithin(r.location_id, f.location, parents)) return false;
    if (needle) {
      const hay = [r.description, r.model_guess, r.subject?.label, ...(r.candidates ?? []).map((c) => c.label)];
      if (!hay.some((h) => h?.toLowerCase().includes(needle))) return false;
    }
    return true;
  });
  const key = f.sort ?? 'created_at';
  const dir = f.order === 'asc' ? 1 : -1;
  if (key === 'observed_at' || out.every((r) => r.created_at)) {
    out = [...out].sort((a, b) => {
      const ta = Date.parse((key === 'created_at' ? a.created_at : a.observed_at) ?? '') || 0;
      const tb = Date.parse((key === 'created_at' ? b.created_at : b.observed_at) ?? '') || 0;
      return (ta - tb) * dir;
    });
  }
  return out;
}

// ---------------------------------------------------------------- entity tiles (AC-UI-13)
interface EntityTileProps {
  entity: ReviewEntity | ReviewCandidate;
  nameOf: (id?: string | null) => string;
  role: 'subject' | 'candidate';
}

function EntityFacts({ entity, nameOf, role }: EntityTileProps) {
  const label = entity.label || `Item ${shortId(entity.entity_id)}`;
  const loc = locationLabel(entity.last_location, entity.last_location_name || nameOf(entity.last_location));
  const similarity = 'similarity' in entity ? entity.similarity : undefined;
  return (
    <div className="entity-facts">
      <Link to={`/items/${entity.entity_id}`} className="entity-label">{label}</Link>{' '}
      <code className="loc-code" title={entity.entity_id}>{shortId(entity.entity_id)}</code>
      {entity.catalog_id && <div className="muted">{`Catalog: ${entity.catalog_id}`}</div>}
      <dl className="mini-facts">
        <dt>Location</dt>
        <dd>{loc || '—'}</dd>
        <dt>Last seen</dt>
        <dd><Timestamp iso={entity.last_seen_at} stacked /></dd>
        <dt>Confidence</dt>
        <dd>{typeof entity.confidence === 'number' ? <ConfidenceBar value={entity.confidence} /> : '—'}</dd>
        <dt>Sightings</dt>
        <dd>{typeof entity.sighting_count === 'number' ? entity.sighting_count : '—'}</dd>
        {role === 'candidate' && (
          <>
            <dt>Similarity</dt>
            <dd>{typeof similarity === 'number' && Number.isFinite(similarity) ? `${Math.round(Math.max(0, Math.min(1, similarity)) * 100)}%` : '—'}</dd>
          </>
        )}
      </dl>
    </div>
  );
}

interface DuplicateProps {
  review: ReviewItem;
  subject: ReviewEntity | null;
  nameOf: (id?: string | null) => string;
  disabled: boolean;
  run: (body: Omit<ResolveRequest, 'operator'>) => Promise<void>;
}

function DuplicateCompare({ review, subject, nameOf, disabled, run }: DuplicateProps) {
  const candidates = review.candidates ?? [];
  const p = `rv-${review.review_id}`;
  const searchFor = subject?.label || review.model_guess || '';
  return (
    <div className="dup-compare">
      <section className="dup-subject" aria-labelledby={`${p}-subj`}>
        <h3 id={`${p}-subj`}>Subject (this sighting)</h3>
        <FrameImage src={review.image_url} alt={`Review image: ${review.description}`} bbox={review.image_bbox} />
        {subject ? (
          <EntityFacts entity={subject} nameOf={nameOf} role="subject" />
        ) : (
          <p className="muted">{review.entity_id ? `Item ${shortId(review.entity_id)} (details unavailable)` : 'No item attached.'}</p>
        )}
      </section>
      <section className="dup-candidates" aria-labelledby={`${p}-cands`}>
        <h3 id={`${p}-cands`}>
          {review.candidates_source === 'suggested' ? 'Possible matches (suggested: nearest same-class items)' : 'Possible matches'}
        </h3>
        {candidates.length ? (
          <ul className="candidates" aria-label="Candidates">
            {candidates.map((c) => {
              const label = c.label || `Item ${shortId(c.entity_id)}`;
              const where = c.last_location ? ` (${c.last_location})` : '';
              return (
                <li key={c.entity_id} className="candidate">
                  <FrameImage src={c.thumbnail_url ?? c.snapshot_url} alt={label} size="md" />
                  <EntityFacts entity={c} nameOf={nameOf} role="candidate" />
                  <button type="button" disabled={disabled}
                    aria-label={`Merge subject into this: ${label} ${shortId(c.entity_id)}${where}`}
                    onClick={() => run({ action: 'merge_into', target_entity_id: c.entity_id })}>
                    Merge subject into this
                  </button>
                </li>
              );
            })}
          </ul>
        ) : (
          <div className="dup-empty" role="note">
            <p>No candidate items were found for this possible duplicate, so there is nothing to merge into.</p>
            <p className="muted">
              {'Compare it with the inventory yourself'}
              {searchFor && (
                <>
                  {': '}
                  <Link to={`/?q=${encodeURIComponent(searchFor)}`}>{`search the inventory for "${searchFor}"`}</Link>
                </>
              )}
              {'. If it is a separate item, choose "Not a duplicate (keep both)".'}
            </p>
          </div>
        )}
      </section>
    </div>
  );
}

// ---------------------------------------------------------------- card
interface CardProps {
  review: ReviewItem;
  catalog: CatalogItem[];
  nameOf: (id?: string | null) => string;
  canAct: boolean;
  resolve: Resolve;
}

function dismissConsequence(review: ReviewItem): string {
  if (!review.entity_id) return 'Dismissing closes this review. No inventory item is attached to it.';
  if (review.kind === 'possible_duplicate') {
    return 'Dismissing closes this review AND marks the subject item as dismissed, which hides it from the default ' +
      'inventory. If both are real items, use "Not a duplicate (keep both)" instead.';
  }
  if (review.kind === 'unknown_object') {
    return 'Not an object? Dismissing closes this review AND marks the item as dismissed, which hides it from the ' +
      'default inventory.';
  }
  return 'Dismissing closes this review AND marks the item as dismissed, which hides it from the default inventory.';
}

function ReviewCard({ review, catalog, nameOf, canAct, resolve }: CardProps) {
  const [busy, setBusy] = useState(false);
  const [query, setQuery] = useState('');
  const [selected, setSelected] = useState('');
  const [showNew, setShowNew] = useState(false);
  const [confirmDismiss, setConfirmDismiss] = useState(false);
  const [newId, setNewId] = useState('');
  const [newClass, setNewClass] = useState('');
  const [newDisplay, setNewDisplay] = useState('');

  const isDup = review.kind === 'possible_duplicate';
  // Older APIs send no `subject`: fall back to GET /items/{entity_id} (cached) for duplicate cards.
  const fetched = useCachedItem(isDup && !review.subject && review.entity_id ? review.entity_id : null);
  const subject: ReviewEntity | null = review.subject ?? fetched ?? null;

  const options = useMemo(() => catalog.filter((c) => catalogMatches(c, query)), [catalog, query]);
  const chosen = options.some((c) => c.catalog_id === selected) ? selected : '';
  const idInvalid = newId !== '' && !CATALOG_ID_PATTERN.test(newId);
  const newValid = CATALOG_ID_PATTERN.test(newId) && newClass.trim() !== '' && newDisplay.trim() !== '';
  const p = `rv-${review.review_id}`;
  const locName = locationLabel(review.location_id, nameOf(review.location_id));
  const disabled = !canAct || busy;
  const keepCatalog = subject?.catalog_id || chosen;

  const run = async (body: Omit<ResolveRequest, 'operator'>) => {
    if (disabled) return;
    setBusy(true);
    try {
      await resolve(body);
    } finally {
      setBusy(false);
    }
  };

  const keepBoth = () =>
    run({
      action: 'assign_catalog',
      catalog_id: keepCatalog,
      ...(subject?.label ? { label: subject.label } : {}),
      note: 'not a duplicate (keep both)',
    });

  return (
    <article className={`card review-card${isDup ? ' review-card-wide' : ''}`} data-testid={`review-${review.review_id}`}
      aria-labelledby={`${p}-h`}>
      <header className="review-head">
        <h2 id={`${p}-h`} className={`kind kind-${review.kind}`}>{humanize(review.kind)}</h2>
        <span className="review-head-meta">
          <span className="muted">Observed </span>
          <Timestamp iso={review.observed_at} />
          {review.entity_id && (
            <>
              <Link to={`/items/${review.entity_id}`} aria-label={`Open item ${subject?.label ?? shortId(review.entity_id)}`}>
                Open item
              </Link>
              <Link to={`/items/${review.entity_id}#history`} aria-label={`History of ${subject?.label ?? shortId(review.entity_id)}`}>
                History
              </Link>
            </>
          )}
        </span>
      </header>
      <div className={isDup ? 'review-body-dup' : 'review-body'}>
        {isDup ? (
          <DuplicateCompare review={review} subject={subject} nameOf={nameOf} disabled={disabled} run={run} />
        ) : (
          <div className="review-media">
            <FrameImage src={review.image_url} alt={`Review image: ${review.description}`} bbox={review.image_bbox} />
            {review.image_bbox && <p className="muted small">The outlined box marks the object. Click to enlarge.</p>}
          </div>
        )}
        <div className="review-info">
          <p className="review-desc">{review.description}</p>
          <dl className="facts">
            <dt>Location</dt>
            <dd>{locName || '—'}</dd>
            <dt>Model guess</dt>
            <dd>{review.model_guess || '—'}</dd>
          </dl>

          <div className="review-actions">
            <div className="field">
              <label htmlFor={`${p}-search`}>Search catalog</label>
              <input id={`${p}-search`} type="search" value={query} onChange={(e) => setQuery(e.target.value)} />
            </div>
            <div className="field">
              <label htmlFor={`${p}-catalog`}>Catalog item</label>
              <select id={`${p}-catalog`} value={chosen} onChange={(e) => setSelected(e.target.value)}>
                <option value="">{options.length ? 'Choose…' : 'No matches'}</option>
                {options.map((c) => <option key={c.catalog_id} value={c.catalog_id}>{c.display_name}</option>)}
              </select>
            </div>
            <div className="actions">
              <button type="button" className="primary" disabled={disabled || !chosen}
                onClick={() => run({ action: 'assign_catalog', catalog_id: chosen })}>
                Classify
              </button>
              <button type="button" aria-expanded={showNew} onClick={() => setShowNew((v) => !v)}>New catalog item</button>
              {isDup && (
                <button type="button" disabled={disabled || !keepCatalog} aria-describedby={`${p}-keep-hint`} onClick={keepBoth}>
                  Not a duplicate (keep both)
                </button>
              )}
              <button type="button" className="danger" disabled={disabled} aria-expanded={confirmDismiss}
                aria-controls={`${p}-dismiss`} onClick={() => setConfirmDismiss((v) => !v)}>
                Dismiss…
              </button>
            </div>
            {isDup && (
              <p id={`${p}-keep-hint`} className="muted small">
                {keepCatalog
                  ? `Keep both: closes this review and keeps the subject as its own item (catalog ${keepCatalog}).`
                  : 'Keep both needs a catalog item for the subject: choose one above first.'}
              </p>
            )}
            {confirmDismiss && (
              <div id={`${p}-dismiss`} className="confirm-dismiss" role="group" aria-label="Confirm dismiss">
                <p>{dismissConsequence(review)}</p>
                <div className="actions">
                  <button type="button" className="danger" disabled={disabled}
                    onClick={() => run({ action: 'dismiss' })}>
                    Confirm dismiss
                  </button>
                  <button type="button" onClick={() => setConfirmDismiss(false)}>Cancel</button>
                </div>
              </div>
            )}
            {showNew && (
              <div className="new-catalog" role="group" aria-label="New catalog item">
                <div className="field">
                  <label htmlFor={`${p}-nid`}>Catalog id</label>
                  <input id={`${p}-nid`} value={newId} aria-invalid={idInvalid} aria-describedby={idInvalid ? `${p}-nid-err` : undefined}
                    onChange={(e) => setNewId(e.target.value)} placeholder="e.g. flashlight-yellow" />
                  {idInvalid && (
                    <p id={`${p}-nid-err`} className="field-error">
                      Invalid catalog id: use lowercase letters, digits and hyphens (2-64 chars).
                    </p>
                  )}
                </div>
                <div className="field">
                  <label htmlFor={`${p}-ncls`}>Class name</label>
                  <input id={`${p}-ncls`} value={newClass} onChange={(e) => setNewClass(e.target.value)} />
                </div>
                <div className="field">
                  <label htmlFor={`${p}-ndisp`}>Display name</label>
                  <input id={`${p}-ndisp`} value={newDisplay} onChange={(e) => setNewDisplay(e.target.value)} />
                </div>
                <button type="button" className="primary" disabled={disabled || !newValid}
                  onClick={() => run({
                    action: 'new_catalog',
                    new_catalog: { catalog_id: newId, class_name: newClass.trim(), display_name: newDisplay.trim() },
                  })}>
                  Create
                </button>
              </div>
            )}
          </div>
        </div>
      </div>
    </article>
  );
}

// ---------------------------------------------------------------- page
export default function ReviewPage() {
  const toasts = useToasts();
  const { count: openCount, byKind, refresh: refreshCount } = useReviewCount();
  const catalog = useCatalog();
  const { nameOf, options: locationOptions, locations } = useLocations();
  const [params, setParams] = useSearchParams();
  const search = params.toString();
  const filters = useMemo(() => reviewFiltersFromSearch(new URLSearchParams(search)), [search]);
  const [loaded, setLoaded] = useState<{ items: ReviewItem[]; total: number | null } | null>(null);
  const [operator, changeOperator] = useOperator();

  // react-router's functional setParams sees the params of the render that created the closure, so a debounced
  // update could overwrite a newer change: always merge into the latest params via a ref.
  const latest = useRef(params);
  latest.current = params;
  const update = (patch: Partial<ReviewFilters>) => {
    const next = reviewFiltersToSearch({ ...reviewFiltersFromSearch(latest.current), ...patch });
    latest.current = next;
    setParams(next, { replace: true });
  };

  // Debounced text search.
  const [text, setText] = useState(filters.q ?? '');
  useEffect(() => {
    if (text.trim() === (filters.q ?? '')) return undefined;
    const t = setTimeout(() => update({ q: text.trim() || undefined }), SEARCH_DEBOUNCE_MS);
    return () => clearTimeout(t);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [text]);

  // The nav badge may be stale (new unknowns from perception): refresh it whenever the queue is opened.
  useEffect(() => {
    refreshCount();
  }, [refreshCount]);

  useEffect(() => {
    const ctrl = new AbortController();
    listReviewPage(filters, undefined, ctrl.signal)
      .then((r) => setLoaded(r))
      .catch((e) => {
        if (ctrl.signal.aborted) return;
        setLoaded({ items: [], total: null });
        toasts.error('Could not load the review queue', e);
      });
    return () => ctrl.abort();
  }, [filters, toasts]);

  // X-Total-Count marks an API that applies the filters itself; otherwise filter/sort client-side.
  const reviews = useMemo(() => {
    if (!loaded) return null;
    return loaded.total !== null ? loaded.items : applyReviewFilters(loaded.items, filters, locations);
  }, [loaded, filters, locations]);

  const filtered = !!(filters.kind || filters.location || filters.q);
  const total = loaded?.total ?? (filtered ? null : openCount);
  const sortValue = `${filters.sort ?? 'created_at'}:${filters.order ?? 'desc'}`;

  const op = operator.trim();
  const drop = (id: string) =>
    setLoaded((cur) => (cur ? { items: cur.items.filter((r) => r.review_id !== id), total: cur.total === null ? null : Math.max(0, cur.total - 1) } : cur));
  const makeResolve = (review: ReviewItem): Resolve => async (body) => {
    if (!op) return;
    try {
      await resolveReview(review.review_id, { ...body, operator: op });
      drop(review.review_id);
      toasts.success(`Review resolved (${humanize(body.action)})`);
      refreshCount();
    } catch (e) {
      if (e instanceof ApiError && e.status === 409) {
        // Another operator resolved it first: retrying can never succeed, so drop the card.
        drop(review.review_id);
        toasts.push('info', `This review item was already resolved by someone else (${e.message}).`);
        refreshCount();
        return;
      }
      toasts.error('Could not resolve review', e);
    }
  };

  return (
    <div className="page review">
      <div className="page-head">
        <h1>Review queue</h1>
        <div className="field operator">
          <label htmlFor="rv-operator">Operator</label>
          <input id="rv-operator" value={operator} autoComplete="name" aria-describedby="rv-operator-msg"
            onChange={(e) => changeOperator(e.target.value)} />
          {!op && <p id="rv-operator-msg" className="field-error">Operator is required for review actions.</p>}
        </div>
      </div>

      <form className="review-filters" aria-label="Review filters" onSubmit={(e) => e.preventDefault()}>
        <div className="kind-tabs" role="group" aria-label="Review kind">
          <button type="button" aria-pressed={!filters.kind} onClick={() => update({ kind: undefined })}>
            {`All${openCount !== null ? ` (${openCount})` : ''}`}
          </button>
          {REVIEW_KINDS.filter((k) => !byKind || (byKind[k.kind] ?? 0) > 0 || filters.kind === k.kind).map((k) => (
            <button key={k.kind} type="button" aria-pressed={filters.kind === k.kind} onClick={() => update({ kind: k.kind })}>
              {`${k.label}${byKind ? ` (${byKind[k.kind] ?? 0})` : ''}`}
            </button>
          ))}
        </div>
        <div className="field">
          <label htmlFor="rv-loc">Location</label>
          <select id="rv-loc" value={filters.location ?? ''} onChange={(e) => update({ location: e.target.value || undefined })}>
            <option value="">Anywhere</option>
            <LocationOptions options={locationOptions} />
          </select>
        </div>
        <div className="field grow">
          <label htmlFor="rv-q">Search reviews</label>
          <input id="rv-q" type="search" value={text} placeholder="description, label…" maxLength={200}
            onChange={(e) => setText(e.target.value)} />
        </div>
        <div className="field">
          <label htmlFor="rv-sort">Sort</label>
          <select id="rv-sort" value={sortValue} onChange={(e) => {
            const s = SORTS.find((x) => x.value === e.target.value);
            update({ sort: s?.sort, order: s?.order });
          }}>
            {SORTS.map((s) => <option key={s.value} value={s.value}>{s.label}</option>)}
          </select>
        </div>
      </form>

      {reviews === null && <p className="muted">Loading review queue…</p>}
      {reviews && reviews.length === 0 && (
        <p className="empty">{filtered ? 'No open review items match these filters.' : 'No open review items. All clear.'}</p>
      )}
      {reviews && total !== null && total > reviews.length && (
        <p className="muted" aria-live="polite">{`Showing ${reviews.length} of ${total} open review items.`}</p>
      )}
      <div className="review-list">
        {reviews?.map((r) => (
          <ReviewCard key={r.review_id} review={r} catalog={catalog} nameOf={nameOf} canAct={!!op} resolve={makeResolve(r)} />
        ))}
      </div>
    </div>
  );
}
