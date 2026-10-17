// AC-UI-7: review queue (unknowns, possible duplicates, low confidence, conflicts) with classify / new catalog item /
// merge / dismiss actions. Resolved cards disappear and the nav badge is refreshed.
import { useEffect, useMemo, useState } from 'react';
import { ApiError, listReview, resolveReview } from '../api/client';
import type { CatalogItem, ResolveRequest, ReviewItem } from '../api/types';
import { Thumb } from '../components/Thumb';
import { formatUtc, humanize } from '../lib/format';
import { useCatalog, useLocations } from '../state/reference';
import { useOperator } from '../state/operator';
import { useReviewCount } from '../state/reviewCount';
import { useToasts } from '../state/toasts';

export const CATALOG_ID_PATTERN = /^[a-z0-9][a-z0-9-]{1,63}$/;

type Resolve = (body: Omit<ResolveRequest, 'operator'>) => Promise<void>;

function catalogMatches(c: CatalogItem, q: string): boolean {
  const needle = q.trim().toLowerCase();
  if (!needle) return true;
  return [c.display_name, c.class_name, c.catalog_id, ...(c.synonyms ?? [])].some((s) => s?.toLowerCase().includes(needle));
}

interface CardProps {
  review: ReviewItem;
  catalog: CatalogItem[];
  locationName: (id?: string | null) => string;
  canAct: boolean;
  resolve: Resolve;
}

function ReviewCard({ review, catalog, locationName, canAct, resolve }: CardProps) {
  const [busy, setBusy] = useState(false);
  const [query, setQuery] = useState('');
  const [selected, setSelected] = useState('');
  const [showNew, setShowNew] = useState(false);
  const [newId, setNewId] = useState('');
  const [newClass, setNewClass] = useState('');
  const [newDisplay, setNewDisplay] = useState('');

  const options = useMemo(() => catalog.filter((c) => catalogMatches(c, query)), [catalog, query]);
  const chosen = options.some((c) => c.catalog_id === selected) ? selected : '';
  const idInvalid = newId !== '' && !CATALOG_ID_PATTERN.test(newId);
  const newValid = CATALOG_ID_PATTERN.test(newId) && newClass.trim() !== '' && newDisplay.trim() !== '';
  const p = `rv-${review.review_id}`;
  const locName = locationName(review.location_id);
  const disabled = !canAct || busy;

  const run = async (body: Omit<ResolveRequest, 'operator'>) => {
    if (disabled) return;
    setBusy(true);
    try {
      await resolve(body);
    } finally {
      setBusy(false);
    }
  };

  const candidates = review.candidates ?? [];

  return (
    <article className="card review-card" data-testid={`review-${review.review_id}`} aria-labelledby={`${p}-h`}>
      <header className="review-head">
        <h2 id={`${p}-h`} className={`kind kind-${review.kind}`}>{humanize(review.kind)}</h2>
        <time dateTime={review.observed_at}>{formatUtc(review.observed_at)}</time>
      </header>
      <div className="review-body">
        <div className="review-media">
          <Thumb src={review.image_url} alt={`Review image: ${review.description}`} size="md" />
          {candidates.length > 0 && (
            <ul className="candidates" aria-label="Candidates">
              {candidates.map((c) => (
                <li key={c.entity_id} className="candidate">
                  <Thumb src={c.snapshot_url} alt={c.label} size="md" />
                  <span className="candidate-label">{c.label}</span>
                  <button type="button" disabled={disabled} onClick={() => run({ action: 'merge_into', target_entity_id: c.entity_id })}>
                    {`Merge into ${c.label}`}
                  </button>
                </li>
              ))}
            </ul>
          )}
        </div>
        <div className="review-info">
          <p className="review-desc">{review.description}</p>
          <dl className="facts">
            <dt>Location</dt>
            <dd>{review.location_id ? (locName ? `${locName} (${review.location_id})` : review.location_id) : '—'}</dd>
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
              <button type="button" className="danger" disabled={disabled} onClick={() => run({ action: 'dismiss' })}>Dismiss</button>
            </div>
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

export default function ReviewPage() {
  const toasts = useToasts();
  const { count: openCount, refresh: refreshCount } = useReviewCount();
  const catalog = useCatalog();
  const { nameOf } = useLocations();
  const [reviews, setReviews] = useState<ReviewItem[] | null>(null);
  const [operator, changeOperator] = useOperator();

  // The nav badge may be stale (new unknowns from perception): refresh it whenever the queue is opened.
  useEffect(() => {
    refreshCount();
  }, [refreshCount]);

  useEffect(() => {
    let live = true;
    listReview('open')
      .then((r) => live && setReviews(r))
      .catch((e) => {
        if (!live) return;
        setReviews([]);
        toasts.error('Could not load the review queue', e);
      });
    return () => {
      live = false;
    };
  }, [toasts]);

  const op = operator.trim();
  const makeResolve = (review: ReviewItem): Resolve => async (body) => {
    if (!op) return;
    try {
      await resolveReview(review.review_id, { ...body, operator: op });
      setReviews((rs) => (rs ?? []).filter((r) => r.review_id !== review.review_id));
      toasts.success(`Review resolved (${humanize(body.action)})`);
      refreshCount();
    } catch (e) {
      if (e instanceof ApiError && e.status === 409) {
        // Another operator resolved it first: retrying can never succeed, so drop the card.
        setReviews((rs) => (rs ?? []).filter((r) => r.review_id !== review.review_id));
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
      {reviews === null && <p className="muted">Loading review queue…</p>}
      {reviews && reviews.length === 0 && <p className="empty">No open review items. All clear.</p>}
      {reviews && openCount !== null && openCount > reviews.length && (
        <p className="muted" aria-live="polite">{`Showing ${reviews.length} of ${openCount} open review items.`}</p>
      )}
      <div className="review-list">
        {reviews?.map((r) => (
          <ReviewCard key={r.review_id} review={r} catalog={catalog} locationName={nameOf} canAct={!!op} resolve={makeResolve(r)} />
        ))}
      </div>
    </div>
  );
}
