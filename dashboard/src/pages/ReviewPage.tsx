// AC-UI-7 / AC-UI-13..16: review queue (unknowns, possible duplicates, low confidence, conflicts) with classify / new
// catalog item / merge / keep both / dismiss actions, queue filters and sort. Resolved cards disappear and the nav badge
// is refreshed.
import { useEffect, useMemo, useRef, useState } from 'react';
import { Link, useSearchParams } from 'react-router-dom';
import { ApiError, listReviewPage, resolveReview, unmergeItem } from '../api/client';
import type { CatalogItem, Location, ResolveRequest, ReviewCandidate, ReviewEntity, ReviewFilters, ReviewItem } from '../api/types';
import { ConfidenceBar } from '../components/ConfidenceBar';
import { FrameImage, validBBox } from '../components/FrameImage';
import { Thumb } from '../components/Thumb';
import { LocationOptions } from '../components/LocationSelect';
import { Timestamp } from '../components/Timestamp';
import { SEARCH_DEBOUNCE_MS } from '../lib/filters';
import { humanize, locationLabel, shortId } from '../lib/format';
import { useCachedItem } from '../state/itemCache';
import { useCatalog, useLocations } from '../state/reference';
import { useOperator } from '../state/operator';
import { useReviewCount } from '../state/reviewCount';
import { errorMessage, useToasts, type ToastOptions } from '../state/toasts';

export const CATALOG_ID_PATTERN = /^[a-z0-9][a-z0-9-]{1,63}$/;

/** What the operator did, in plain words, for the result toast (UI2-15); built by the card, not from the API action. */
export interface ResolveOutcome {
  message: string;
  /** Merge only: the merged subject (Undo calls unmerge on it) and the target (toast link). */
  undoEntityId?: string;
  undoLabel?: string;
  link?: ToastOptions['link'];
}
type ResolveBody = Omit<ResolveRequest, 'operator'>;
type Resolve = (body: ResolveBody, outcome: ResolveOutcome) => Promise<void>;

/** Candidate tiles shown before "Show N more" (keeps the card's actions near the top at 1024 px). */
export const COMPACT_CANDIDATES = 4;

/** "headset 55555555" (label + short id), or "item 55555555". */
function entityName(e: { entity_id: string; label?: string | null } | null | undefined, fallbackId?: string | null): string {
  const id = e?.entity_id ?? fallbackId ?? '';
  const label = e?.label?.trim();
  return label ? `${label} ${shortId(id)}` : `item ${shortId(id)}`;
}

const finite = (v: unknown): v is number => typeof v === 'number' && Number.isFinite(v);

/** UI2-06: candidates with a score first, highest score first; the others keep the API order. */
export function rankCandidates(cands: ReviewCandidate[]): ReviewCandidate[] {
  return cands
    .map((c, i) => ({ c, i }))
    .sort((a, b) => {
      const sa = finite(a.c.score) ? a.c.score : -Infinity;
      const sb = finite(b.c.score) ? b.c.score : -Infinity;
      return sb === sa ? a.i - b.i : sb - sa;
    })
    .map((x) => x.c);
}

/** Match reasons that are short plain strings (others dropped). */
function reasonsOf(c: ReviewCandidate): string[] {
  if (!Array.isArray(c.match_reasons)) return [];
  return c.match_reasons
    .filter((r): r is string => typeof r === 'string' && r.trim() !== '')
    .map((r) => r.trim().slice(0, 60))
    .slice(0, 6);
}

/** 0.92 -> "92%"; scores outside 0..1 are shown as numbers. */
function formatScore(v: number): string {
  return v >= 0 && v <= 1 ? `${Math.round(v * 100)}%` : v.toFixed(2);
}

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
  const score = 'score' in entity ? entity.score : undefined;
  const reasons = role === 'candidate' ? reasonsOf(entity as ReviewCandidate) : [];
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
            {finite(score) && (
              <>
                <dt>Match score</dt>
                <dd>{formatScore(score)}</dd>
              </>
            )}
            <dt>Similarity</dt>
            <dd>{typeof similarity === 'number' && Number.isFinite(similarity) ? `${Math.round(Math.max(0, Math.min(1, similarity)) * 100)}%` : '—'}</dd>
          </>
        )}
      </dl>
      {reasons.length > 0 && (
        <ul className="reason-chips" aria-label="Why it matched">
          {reasons.map((r, i) => <li key={i} className="reason-chip">{r}</li>)}
        </ul>
      )}
    </div>
  );
}

interface DuplicateProps {
  review: ReviewItem;
  subject: ReviewEntity | null;
  nameOf: (id?: string | null) => string;
  disabled: boolean;
  /** Opens the merge confirm step for this candidate (UI2-05); nothing is posted yet. */
  onMerge: (c: ReviewCandidate) => void;
}

/** The candidate image: the snapshot with its box when the API sends one (UI2-07), else the thumbnail. */
function CandidateImage({ c, label }: { c: ReviewCandidate; label: string }) {
  if (c.snapshot_url && validBBox(c.snapshot_bbox)) {
    return <FrameImage src={c.snapshot_url} alt={label} size="md" bbox={c.snapshot_bbox} bboxSource={c.snapshot_bbox_source} caption="compact" />;
  }
  return <FrameImage src={c.thumbnail_url ?? c.snapshot_url} alt={label} size="md" caption="compact" />;
}

function candidatesHeading(review: ReviewItem, candidates: ReviewCandidate[]): string {
  if (candidates.some((c) => finite(c.score))) return 'Possible matches (ranked by match score)';
  if (review.candidates_source === 'suggested') {
    return candidates.some((c) => finite(c.similarity))
      ? 'Possible matches (suggested: nearest same-class items)'
      : 'Other items of this class (suggested, unranked)';
  }
  return 'Possible matches';
}

function DuplicateCompare({ review, subject, nameOf, disabled, onMerge }: DuplicateProps) {
  const candidates = useMemo(() => rankCandidates(review.candidates ?? []), [review.candidates]);
  const [showAll, setShowAll] = useState(false);
  const shown = showAll ? candidates : candidates.slice(0, COMPACT_CANDIDATES);
  const hidden = candidates.length - shown.length;
  const p = `rv-${review.review_id}`;
  const searchFor = subject?.label || review.model_guess || '';
  // UI2-14: every card has its own landmark names (subject + review id), so no two regions share a name.
  const who = `${entityName(subject, review.entity_id)}, review ${shortId(review.review_id)}`;
  return (
    <div className="dup-compare">
      <section className="dup-subject" aria-label={`Subject: ${who}`}>
        <h3 id={`${p}-subj`}>Subject (this sighting)</h3>
        <FrameImage src={review.image_url} alt={`Review image: ${review.description}`} bbox={review.image_bbox}
          bboxSource={review.image_bbox_source} caption />
        {subject ? (
          <EntityFacts entity={subject} nameOf={nameOf} role="subject" />
        ) : (
          <p className="muted">{review.entity_id ? `Item ${shortId(review.entity_id)} (details unavailable)` : 'No item attached.'}</p>
        )}
      </section>
      <section className="dup-candidates" aria-label={`Possible matches for ${who}`}>
        <h3 id={`${p}-cands`}>{candidatesHeading(review, candidates)}</h3>
        {candidates.length ? (
          <>
            <ul className="candidates" aria-label="Candidates">
              {shown.map((c) => {
                const label = c.label || `Item ${shortId(c.entity_id)}`;
                const where = c.last_location ? ` (${c.last_location})` : '';
                return (
                  <li key={c.entity_id} className="candidate">
                    <CandidateImage c={c} label={label} />
                    <EntityFacts entity={c} nameOf={nameOf} role="candidate" />
                    <button type="button" disabled={disabled}
                      aria-label={`Merge subject into this: ${label} ${shortId(c.entity_id)}${where}`}
                      onClick={() => onMerge(c)}>
                      Merge subject into this
                    </button>
                  </li>
                );
              })}
            </ul>
            {candidates.length > COMPACT_CANDIDATES && (
              <button type="button" className="link-button show-more" aria-expanded={showAll} onClick={() => setShowAll((v) => !v)}>
                {showAll ? 'Show fewer candidates' : `Show ${hidden} more candidates`}
              </button>
            )}
          </>
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

/** UI2-05: the merge confirm step: both images and what the merge does. */
function MergeConfirm({ review, subject, target, nameOf, disabled, onConfirm, onCancel }: {
  review: ReviewItem;
  subject: ReviewEntity | null;
  target: ReviewCandidate;
  nameOf: (id?: string | null) => string;
  disabled: boolean;
  onConfirm: () => void;
  onCancel: () => void;
}) {
  const confirmRef = useRef<HTMLButtonElement>(null);
  useEffect(() => {
    confirmRef.current?.focus();
  }, [target.entity_id]);
  const subj = entityName(subject, review.entity_id);
  const tgt = entityName(target);
  const tgtLoc = locationLabel(target.last_location, target.last_location_name || nameOf(target.last_location));
  return (
    <div className="confirm-merge" role="group" aria-label="Confirm merge">
      <div className="confirm-merge-images">
        <figure>
          <Thumb src={subject?.thumbnail_url ?? subject?.snapshot_url ?? review.image_url} alt={`Subject: ${subj}`} size="md" />
          <figcaption className="muted small">{subj}</figcaption>
        </figure>
        <span aria-hidden="true" className="confirm-merge-arrow">→</span>
        <figure>
          <Thumb src={target.thumbnail_url ?? target.snapshot_url} alt={`Target: ${tgt}`} size="md" />
          <figcaption className="muted small">{tgt}</figcaption>
        </figure>
      </div>
      <p>
        {`Merge ${subj} into ${tgt}${tgtLoc ? ` at ${tgtLoc}` : ''}? ${subj} is marked merged, and its sightings and history ` +
          `move to ${tgt}. You can undo this from the notification or from the item page.`}
      </p>
      <div className="actions">
        <button ref={confirmRef} type="button" className="primary" disabled={disabled} onClick={onConfirm}>Confirm merge</button>
        <button type="button" onClick={onCancel}>Cancel</button>
      </div>
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
  const [mergeTarget, setMergeTarget] = useState<ReviewCandidate | null>(null);
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

  const run = async (body: ResolveBody, outcome: ResolveOutcome) => {
    if (disabled) return;
    setBusy(true);
    try {
      await resolve(body, outcome);
    } finally {
      setBusy(false);
    }
  };

  const subj = entityName(subject, review.entity_id);
  const catalogName = (id: string) => catalog.find((c) => c.catalog_id === id)?.display_name ?? id;

  const keepBoth = () =>
    run(
      {
        action: 'assign_catalog',
        catalog_id: keepCatalog,
        ...(subject?.label ? { label: subject.label } : {}),
        note: 'not a duplicate (keep both)',
      },
      { message: `Kept both items: ${subj} stays a separate item.` },
    );

  const confirmMerge = (target: ReviewCandidate) => {
    const tgt = entityName(target);
    return run(
      { action: 'merge_into', target_entity_id: target.entity_id },
      {
        message: `Merged ${subj} into ${tgt}.`,
        undoEntityId: review.entity_id ?? undefined,
        undoLabel: subj,
        link: { to: `/items/${target.entity_id}`, label: `Open ${tgt}` },
      },
    );
  };

  const actions = (
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
          onClick={() => run({ action: 'assign_catalog', catalog_id: chosen }, { message: `Classified as ${catalogName(chosen)}.` })}>
          Classify
        </button>
        <button type="button" aria-expanded={showNew} onClick={() => setShowNew((v) => !v)}>New catalog item</button>
        {isDup && (
          <button type="button" disabled={disabled || !keepCatalog} aria-describedby={`${p}-keep-hint`} onClick={keepBoth}>
            Not a duplicate (keep both)
          </button>
        )}
        <button type="button" className="danger" disabled={disabled} aria-expanded={confirmDismiss}
          aria-controls={`${p}-dismiss`} onClick={() => {
            setMergeTarget(null);
            setConfirmDismiss((v) => !v);
          }}>
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
      {mergeTarget && (
        <MergeConfirm review={review} subject={subject} target={mergeTarget} nameOf={nameOf} disabled={disabled}
          onConfirm={() => void confirmMerge(mergeTarget)} onCancel={() => setMergeTarget(null)} />
      )}
      {confirmDismiss && (
        <div id={`${p}-dismiss`} className="confirm-dismiss" role="group" aria-label="Confirm dismiss">
          <p>{dismissConsequence(review)}</p>
          <div className="actions">
            <button type="button" className="danger" disabled={disabled}
              onClick={() => run({ action: 'dismiss' }, {
                message: review.entity_id ? `Dismissed ${subj}: it is hidden from the default inventory.` : 'Dismissed the review.',
              })}>
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
            }, { message: `Created catalog item ${newDisplay.trim()} and classified the item as it.` })}>
            Create
          </button>
        </div>
      )}
    </div>
  );

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
          <DuplicateCompare review={review} subject={subject} nameOf={nameOf} disabled={disabled}
            onMerge={(c) => {
              setConfirmDismiss(false);
              setMergeTarget(c);
            }} />
        ) : (
          <div className="review-media">
            <FrameImage src={review.image_url} alt={`Review image: ${review.description}`} bbox={review.image_bbox}
              bboxSource={review.image_bbox_source} caption />
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

          {!isDup && actions}
        </div>
      </div>
      {isDup && <div className="review-footer">{actions}</div>}
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
  const [reloadKey, setReloadKey] = useState(0);

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
  }, [filters, toasts, reloadKey]);

  // X-Total-Count marks an API that applies the filters itself; otherwise filter/sort client-side.
  const reviews = useMemo(() => {
    if (!loaded) return null;
    return loaded.total !== null ? loaded.items : applyReviewFilters(loaded.items, filters, locations);
  }, [loaded, filters, locations]);

  const filtered = !!(filters.kind || filters.location || filters.q);
  // Matching = what the filters select (X-Total-Count, else the client-filtered list); rendered may be fewer (limit).
  const matching = reviews ? loaded?.total ?? (filtered ? reviews.length : openCount ?? reviews.length) : null;
  let countLine: string | null = null;
  if (reviews && matching !== null && matching > reviews.length) {
    countLine = `Showing ${reviews.length} of ${matching} matching review items.`;
  } else if (reviews && filtered && matching !== null) {
    // UI2-13: a filtered queue always says how much of the queue it shows.
    countLine = openCount !== null ? `Showing ${matching} of ${openCount} open review items.` : `${matching} matching review items.`;
  }
  const sortValue = `${filters.sort ?? 'created_at'}:${filters.order ?? 'desc'}`;

  const op = operator.trim();
  const drop = (id: string) =>
    setLoaded((cur) => (cur ? { items: cur.items.filter((r) => r.review_id !== id), total: cur.total === null ? null : Math.max(0, cur.total - 1) } : cur));
  /** UI2-05: Undo from the merge toast: unmerge the subject, then reload the queue (the review may reopen). */
  const undoMerge = (entityId: string, label: string, who: string) => {
    unmergeItem(entityId, { operator: who })
      .then(() => {
        toasts.success(`Merge undone: ${label} is a separate item again.`);
        setReloadKey((k) => k + 1);
        refreshCount();
      })
      .catch((e) => {
        if (e instanceof ApiError && e.status === 409) {
          toasts.push('error', `This merge can't be undone safely: ${errorMessage(e)}. Nothing was changed.`);
        } else {
          toasts.error('Could not undo the merge', e);
        }
      });
  };
  const makeResolve = (review: ReviewItem): Resolve => async (body, outcome) => {
    if (!op) return;
    try {
      await resolveReview(review.review_id, { ...body, operator: op });
      drop(review.review_id);
      const undoId = outcome.undoEntityId;
      toasts.success(outcome.message, {
        link: outcome.link,
        action: undoId ? { label: 'Undo merge', onClick: () => undoMerge(undoId, outcome.undoLabel ?? 'the item', op) } : undefined,
      });
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
      {filtered && <p className="muted small">Tab counts are totals for the whole queue, not for the current filters.</p>}

      {reviews === null && <p className="muted">Loading review queue…</p>}
      {reviews && reviews.length === 0 && (
        <p className="empty">{filtered ? 'No open review items match these filters.' : 'No open review items. All clear.'}</p>
      )}
      {countLine && <p className="muted review-count" aria-live="polite">{countLine}</p>}
      <div className="review-list">
        {reviews?.map((r) => (
          <ReviewCard key={r.review_id} review={r} catalog={catalog} nameOf={nameOf} canAct={!!op} resolve={makeResolve(r)} />
        ))}
      </div>
    </div>
  );
}
