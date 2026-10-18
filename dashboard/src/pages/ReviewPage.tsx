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
import { appearanceChips, appearanceOf, displayLabel, displayLabelWithId, isPerson, personAppearance, reviewReason } from '../lib/person';
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

/** "headset 55555555" (label + short id), "Person: grey hoodie 87dbd193" (UI3-03), or "item 55555555". */
function entityName(e: ReviewEntity | null | undefined, fallbackId?: string | null): string {
  return displayLabelWithId(e ?? { entity_id: fallbackId ?? '' });
}

/** UI3-08: catalog display name for a slug (the slug itself when the catalog does not list it). */
type CatalogName = (id: string) => string;

/** Person text shown when a person review frame has no box (UI3-05). */
export const PERSON_NO_BOX = 'Whole frame, several people may be visible';

/**
 * UI5-01: the short name of what an unknown review describes: the "label:" prefix of the description ("orange bag: a
 * small orange bag" -> "orange bag"), else the model guess, else the description (cut at 40 characters).
 */
export function unknownLabel(review: Pick<ReviewItem, 'description' | 'model_guess'>): string {
  const d = (review.description ?? '').replace(/\s+/g, ' ').trim();
  const head = /^([^:]{1,40}):/.exec(d)?.[1].trim();
  if (head) return head;
  const guess = review.model_guess?.trim();
  if (guess) return guess;
  return d.length > 40 ? `${d.slice(0, 40).trimEnd()}…` : d || 'the object';
}

/** UI5-01: caption for an unknown with no box: what to look for, and nothing is highlighted. */
export const unknownNoBox = (review: Pick<ReviewItem, 'description' | 'model_guess'>) =>
  `No box: look for ${unknownLabel(review)} in the frame`;

/** UI5-01: for each unknown review, the labels of the other open unknowns that share its frame (same image_url). */
export function sameFrameOthers(reviews: ReviewItem[]): Map<string, string[]> {
  const byFrame = new Map<string, ReviewItem[]>();
  for (const r of reviews) {
    if (r.kind !== 'unknown_object' || !r.image_url) continue;
    byFrame.set(r.image_url, [...(byFrame.get(r.image_url) ?? []), r]);
  }
  const out = new Map<string, string[]>();
  for (const group of byFrame.values()) {
    if (group.length < 2) continue;
    for (const r of group) out.set(r.review_id, group.filter((o) => o !== r).map(unknownLabel));
  }
  return out;
}

/** UI3-05: colour + garment chips from the appearance, so outfits can be compared side by side. */
function AppearanceChips({ text }: { text: string }) {
  const chips = appearanceChips(text);
  if (!chips.length) return null;
  return (
    <ul className="reason-chips appearance-chips" aria-label="Appearance">
      {chips.map((c) => <li key={c} className="reason-chip appearance-chip">{c}</li>)}
    </ul>
  );
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
  catalogName: CatalogName;
}

function EntityFacts({ entity, nameOf, role, catalogName }: EntityTileProps) {
  const person = isPerson(entity);
  const label = person || entity.label ? displayLabel(entity) : `Item ${shortId(entity.entity_id)}`;
  const loc = locationLabel(entity.last_location, entity.last_location_name || nameOf(entity.last_location));
  const similarity = 'similarity' in entity ? entity.similarity : undefined;
  const score = 'score' in entity ? entity.score : undefined;
  const reasons = role === 'candidate' ? reasonsOf(entity as ReviewCandidate) : [];
  return (
    <div className="entity-facts">
      <Link to={`/items/${entity.entity_id}`} className="entity-label">{label}</Link>{' '}
      <code className="loc-code" title={entity.entity_id}>{shortId(entity.entity_id)}</code>
      {entity.catalog_id && (
        <div className="muted" title={entity.catalog_id}>{`Catalog: ${entity.catalog_name || catalogName(entity.catalog_id)}`}</div>
      )}
      {person && <AppearanceChips text={appearanceOf(entity)} />}
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
  /** UI3-05: the review is about a person (scene frames, appearance labels, "several people" caption). */
  person: boolean;
  /** Review description without internal text (UI3-05). */
  text: string;
  catalogName: CatalogName;
  nameOf: (id?: string | null) => string;
  disabled: boolean;
  /** Opens the merge confirm step for this candidate (UI2-05); nothing is posted yet. */
  onMerge: (c: ReviewCandidate) => void;
}

/** The candidate image: the snapshot with its box when the API sends one (UI2-07), else the thumbnail. */
function CandidateImage({ c, label, person }: { c: ReviewCandidate; label: string; person: boolean }) {
  if (c.snapshot_url && validBBox(c.snapshot_bbox)) {
    return <FrameImage src={c.snapshot_url} alt={label} size="md" bbox={c.snapshot_bbox} bboxSource={c.snapshot_bbox_source} caption="compact"
      person={person} />;
  }
  return <FrameImage src={c.thumbnail_url ?? c.snapshot_url} alt={label} size="md" caption="compact" person={person}
    noBoxCaption={person ? PERSON_NO_BOX : undefined} />;
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

function DuplicateCompare({ review, subject, person, text, catalogName, nameOf, disabled, onMerge }: DuplicateProps) {
  const candidates = useMemo(() => rankCandidates(review.candidates ?? []), [review.candidates]);
  const [showAll, setShowAll] = useState(false);
  const shown = showAll ? candidates : candidates.slice(0, COMPACT_CANDIDATES);
  const hidden = candidates.length - shown.length;
  const p = `rv-${review.review_id}`;
  const searchFor = (person ? '' : subject?.label) || review.model_guess || '';
  // UI2-14: every card has its own landmark names (subject + review id), so no two regions share a name.
  const who = `${entityName(subject, review.entity_id)}, review ${shortId(review.review_id)}`;
  return (
    <div className="dup-compare">
      <section className="dup-subject" aria-label={`Subject: ${who}`}>
        <h3 id={`${p}-subj`}>Subject (this sighting)</h3>
        <FrameImage src={review.image_url} alt={`Review image: ${text}`} bbox={review.image_bbox}
          bboxSource={review.image_bbox_source} caption person={person} noBoxCaption={person ? PERSON_NO_BOX : undefined} />
        {subject ? (
          <EntityFacts entity={subject} nameOf={nameOf} role="subject" catalogName={catalogName} />
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
                const cand = person && !c.kind ? { ...c, kind: 'person' as const } : c;
                const label = isPerson(cand) || c.label ? displayLabel(cand) : `Item ${shortId(c.entity_id)}`;
                const where = c.last_location ? ` (${c.last_location})` : '';
                return (
                  <li key={c.entity_id} className="candidate">
                    <CandidateImage c={c} label={label} person={isPerson(cand)} />
                    <EntityFacts entity={cand} nameOf={nameOf} role="candidate" catalogName={catalogName} />
                    <button type="button" disabled={disabled}
                      aria-label={`Merge subject into this: ${displayLabelWithId(cand)}${where}`}
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
          <Thumb src={subject?.thumbnail_url ?? subject?.snapshot_url ?? review.image_url} alt={`Subject: ${subj}`} size="md" person={isPerson(subject)} />
          <figcaption className="muted small">{subj}</figcaption>
        </figure>
        <span aria-hidden="true" className="confirm-merge-arrow">→</span>
        <figure>
          <Thumb src={target.thumbnail_url ?? target.snapshot_url} alt={`Target: ${tgt}`} size="md" person={isPerson(target) || isPerson(subject)} />
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
  /** UI5-01: labels of the other unknowns on the same frame. */
  sameFrame?: string[];
  /** UI5-02: bulk selection on the unknown tab (absent elsewhere). */
  selection?: { checked: boolean; onChange: (checked: boolean) => void };
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

/** UI4-05: the standard dismiss note for "Not inventory (station structure)" (the resolve contract takes a free `note`). */
export const NOT_INVENTORY_NOTE = 'not_inventory';

function ReviewCard({ review, catalog, nameOf, canAct, resolve, sameFrame, selection }: CardProps) {
  const [busy, setBusy] = useState(false);
  const [query, setQuery] = useState('');
  const [selected, setSelected] = useState('');
  const [showNew, setShowNew] = useState(false);
  const [confirmDismiss, setConfirmDismiss] = useState(false);
  const [confirmStructure, setConfirmStructure] = useState(false);
  const [showClassify, setShowClassify] = useState(false);
  const [mergeTarget, setMergeTarget] = useState<ReviewCandidate | null>(null);
  const [newId, setNewId] = useState('');
  const [newClass, setNewClass] = useState('');
  const [newDisplay, setNewDisplay] = useState('');

  const isDup = review.kind === 'possible_duplicate';
  // Older APIs send no `subject`: fall back to GET /items/{entity_id} (cached) for duplicate cards.
  const fetched = useCachedItem(isDup && !review.subject && review.entity_id ? review.entity_id : null);
  const rawSubject: ReviewEntity | null = review.subject ?? fetched ?? null;
  // UI3-05: internal reconciler text is rewritten into a readable reason; UI3-03: a person subject is named by its
  // appearance (its own description, else the appearance in the review description).
  const { text, reason } = reviewReason(review.description);
  const person = isPerson(rawSubject) || review.model_guess === 'person' || /^person\b/i.test(text);
  const subject: ReviewEntity | null = rawSubject && person
    ? { ...rawSubject, kind: 'person', description: rawSubject.description || personAppearance(text) || null }
    : rawSubject;

  const options = useMemo(() => catalog.filter((c) => catalogMatches(c, query)), [catalog, query]);
  const chosen = options.some((c) => c.catalog_id === selected) ? selected : '';
  const idInvalid = newId !== '' && !CATALOG_ID_PATTERN.test(newId);
  const newValid = CATALOG_ID_PATTERN.test(newId) && newClass.trim() !== '' && newDisplay.trim() !== '';
  const p = `rv-${review.review_id}`;
  const locName = locationLabel(review.location_id, nameOf(review.location_id));
  const disabled = !canAct || busy;
  const keepCatalog = subject?.catalog_id || chosen;
  // UI5-01: an unknown with no box says what to look for (nothing is highlighted).
  const noBoxCaption = person ? PERSON_NO_BOX : review.kind === 'unknown_object' ? unknownNoBox(review) : undefined;

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
  const catalogName: CatalogName = (id) => catalog.find((c) => c.catalog_id === id)?.display_name ?? id;

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

  const catalogFields = (
    <>
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
    </>
  );
  const classifyButton = (
    <button type="button" className="primary" disabled={disabled || !chosen}
      onClick={() => run({ action: 'assign_catalog', catalog_id: chosen }, { message: `Classified as ${catalogName(chosen)}.` })}>
      Classify
    </button>
  );
  const newButton = <button type="button" aria-expanded={showNew} onClick={() => setShowNew((v) => !v)}>New catalog item</button>;
  const dismissButton = (
    <button type="button" className="danger" disabled={disabled} aria-expanded={confirmDismiss}
      aria-controls={`${p}-dismiss`} onClick={() => {
        setMergeTarget(null);
        setConfirmStructure(false);
        setConfirmDismiss((v) => !v);
      }}>
      Dismiss…
    </button>
  );
  // UI4-05: unknowns that are station structure (window, hatch, handrail…) are closed in one step, with a standard note.
  const structureButton = review.kind === 'unknown_object' ? (
    <button type="button" disabled={disabled} aria-expanded={confirmStructure} aria-controls={`${p}-structure`}
      onClick={() => {
        setConfirmDismiss(false);
        setConfirmStructure((v) => !v);
      }}>
      Not inventory (station structure)…
    </button>
  ) : null;
  const keepHint = keepCatalog
    ? `Keep both: closes this review and keeps the subject as its own item (catalog ${catalogName(keepCatalog)}).`
    : 'Keep both needs a catalog item for the subject: open "Choose catalog…" and pick one first.';

  const confirms = (
    <>
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
      {confirmStructure && (
        <div id={`${p}-structure`} className="confirm-dismiss" role="group" aria-label="Confirm not inventory">
          <p>
            {'Station structure (a window, hatch, handrail, panel or visiting vehicle) is not inventory. This closes the ' +
              'review with the note "not_inventory"' +
              (review.entity_id ? ' AND marks the item as dismissed, which hides it from the default inventory.' : '. No inventory item is attached to it.')}
          </p>
          <div className="actions">
            <button type="button" className="danger" disabled={disabled}
              onClick={() => run({ action: 'dismiss', note: NOT_INVENTORY_NOTE }, {
                message: review.entity_id
                  ? `Marked ${subj} as station structure (not inventory): it is hidden from the default inventory.`
                  : 'Marked as station structure (not inventory): the review is closed.',
              })}>
              Confirm: not inventory
            </button>
            <button type="button" onClick={() => setConfirmStructure(false)}>Cancel</button>
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
    </>
  );

  const actions = (
    <div className="review-actions">
      {catalogFields}
      <div className="actions">
        {classifyButton}
        {newButton}
        {structureButton}
        {dismissButton}
      </div>
      {confirms}
    </div>
  );

  // UI3-09: duplicate cards get one compact row (at most 64 px); the catalog search opens on demand, so the
  // candidates and their Merge buttons stay visible at 1024×768.
  const dupFooter = (
    <>
      <div className="review-footer-row">
        <button type="button" aria-expanded={showClassify} aria-controls={`${p}-classify`} onClick={() => setShowClassify((v) => !v)}>
          Choose catalog…
        </button>
        {newButton}
        <button type="button" disabled={disabled || !keepCatalog} aria-describedby={`${p}-keep-hint`} title={keepHint} onClick={keepBoth}>
          Not a duplicate (keep both)
        </button>
        {dismissButton}
      </div>
      <p id={`${p}-keep-hint`} className="keep-hint">{keepHint}</p>
      {showClassify && (
        <div id={`${p}-classify`} className="review-actions review-classify" role="group" aria-label="Classify with a catalog item">
          {catalogFields}
          <div className="actions">{classifyButton}</div>
        </div>
      )}
      {confirms}
    </>
  );

  return (
    <article className={`card review-card${isDup ? ' review-card-wide' : ''}`} data-testid={`review-${review.review_id}`}
      aria-labelledby={`${p}-h`}>
      <header className="review-head">
        <span className="review-head-title">
          {selection && (
            <span className="bulk-select">
              <input id={`${p}-sel`} type="checkbox" checked={selection.checked} onChange={(e) => selection.onChange(e.target.checked)} />
              <label htmlFor={`${p}-sel`}>
                {'Select '}<span className="sr-only">{`${unknownLabel(review)} (${shortId(review.review_id)}) for bulk action`}</span>
              </label>
            </span>
          )}
          <h2 id={`${p}-h`} className={`kind kind-${review.kind}`}>{humanize(review.kind)}</h2>
        </span>
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
          <DuplicateCompare review={review} subject={subject} person={person} text={text} catalogName={catalogName}
            nameOf={nameOf} disabled={disabled}
            onMerge={(c) => {
              setConfirmDismiss(false);
              setMergeTarget(c);
            }} />
        ) : (
          <div className="review-media">
            <FrameImage src={review.image_url} alt={`Review image: ${text}`} bbox={review.image_bbox}
              bboxSource={review.image_bbox_source} caption person={person} noBoxCaption={noBoxCaption} />
            {sameFrame && sameFrame.length > 0 && (
              <p className="same-frame-note small">
                {`Same frame as ${sameFrame.length} other unknown${sameFrame.length === 1 ? '' : 's'} (${sameFrame.join(', ')})`}
              </p>
            )}
          </div>
        )}
        <div className="review-info">
          <p className="review-desc">{text}</p>
          {reason && <p className="review-reason">{reason}</p>}
          <dl className="facts">
            <dt>Location</dt>
            <dd>{locName || '—'}</dd>
            <dt>Model guess</dt>
            <dd>{review.model_guess || '—'}</dd>
          </dl>

          {!isDup && actions}
        </div>
      </div>
      {isDup && <div className="review-footer">{dupFooter}</div>}
    </article>
  );
}

// ---------------------------------------------------------------- bulk "Not inventory" (UI5-02)
/**
 * UI5-02: multi-select unknowns (structure the denylist missed) and dismiss them with the note "not_inventory" after a
 * confirm step. Native checkboxes and buttons (keyboard by default); Esc closes the confirm and returns focus.
 */
function BulkNotInventory({ reviews, selected, setSelected, operator, onDone }: {
  reviews: ReviewItem[];
  selected: Set<string>;
  setSelected: (s: Set<string>) => void;
  operator: string;
  /** Called with the review ids that are closed (resolved now, or already resolved by someone else). */
  onDone: (ids: string[]) => void;
}) {
  const toasts = useToasts();
  const [confirming, setConfirming] = useState(false);
  const [busy, setBusy] = useState(false);
  const openRef = useRef<HTMLButtonElement>(null);
  const confirmRef = useRef<HTMLButtonElement>(null);
  const allRef = useRef<HTMLInputElement>(null);
  const unknowns = reviews.filter((r) => r.kind === 'unknown_object');
  const chosen = unknowns.filter((r) => selected.has(r.review_id));
  const n = chosen.length;
  const all = n > 0 && n === unknowns.length;
  useEffect(() => {
    if (allRef.current) allRef.current.indeterminate = n > 0 && !all;
  }, [n, all]);
  useEffect(() => {
    if (confirming) confirmRef.current?.focus();
  }, [confirming]);
  useEffect(() => {
    if (n === 0) setConfirming(false);
  }, [n]);
  const close = () => {
    setConfirming(false);
    setTimeout(() => openRef.current?.focus(), 0);
  };
  const run = async () => {
    if (!operator || busy || !n) return;
    setBusy(true);
    const done: string[] = [];
    let failed = 0;
    for (const r of chosen) {
      try {
        await resolveReview(r.review_id, { action: 'dismiss', note: NOT_INVENTORY_NOTE, operator });
        done.push(r.review_id);
      } catch (e) {
        if (e instanceof ApiError && e.status === 409) done.push(r.review_id);
        else failed += 1;
      }
    }
    setBusy(false);
    setConfirming(false);
    onDone(done);
    const ok = n - failed;
    if (ok > 0) {
      toasts.success(`Marked ${ok} item${ok === 1 ? '' : 's'} as station structure (not inventory): hidden from the default inventory.`);
    }
    if (failed > 0) {
      toasts.push('error', `${failed} of ${n} could not be marked as not inventory. Their cards stay in the queue.`);
    }
  };
  return (
    <div className="bulk-bar card" role="group" aria-label="Bulk actions">
      <div className="bulk-row">
        <span className="bulk-select">
          <input ref={allRef} id="rv-bulk-all" type="checkbox" checked={all}
            onChange={(e) => setSelected(new Set(e.target.checked ? unknowns.map((r) => r.review_id) : []))} />
          <label htmlFor="rv-bulk-all">Select all shown unknowns</label>
        </span>
        <span className="muted" aria-live="polite">{`${n} selected`}</span>
        <button ref={openRef} type="button" disabled={!n || !operator || busy} aria-expanded={confirming}
          aria-controls="rv-bulk-confirm" onClick={() => setConfirming((v) => !v)}>
          {`Not inventory (${n} selected)…`}
        </button>
        {!operator && <span className="muted small">Enter an operator name to use bulk actions.</span>}
      </div>
      {confirming && n > 0 && (
        <div id="rv-bulk-confirm" className="confirm-dismiss" role="group" aria-label={`Confirm not inventory for ${n} items`}
          onKeyDown={(e) => {
            if (e.key === 'Escape') {
              e.stopPropagation();
              close();
            }
          }}>
          <p>
            {`Station structure is not inventory. This closes ${n} review${n === 1 ? '' : 's'} with the note ` +
              '"not_inventory" and marks any attached items as dismissed (hidden from the default inventory):'}
          </p>
          <ul className="bulk-list">
            {chosen.map((r) => <li key={r.review_id}>{`${unknownLabel(r)} (${shortId(r.review_id)})`}</li>)}
          </ul>
          <div className="actions">
            <button ref={confirmRef} type="button" className="danger" disabled={busy || !operator} onClick={() => void run()}>
              {busy ? 'Marking…' : `Confirm: not inventory (${n})`}
            </button>
            <button type="button" onClick={close}>Cancel</button>
          </div>
        </div>
      )}
    </div>
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
  const sameFrame = useMemo(() => sameFrameOthers(reviews ?? []), [reviews]);
  const bulkTab = filters.kind === 'unknown_object';
  const [selected, setSelected] = useState<Set<string>>(() => new Set());
  const setOne = (id: string, on: boolean) =>
    setSelected((cur) => {
      const next = new Set(cur);
      if (on) next.add(id);
      else next.delete(id);
      return next;
    });
  const drop = (id: string) =>
    setLoaded((cur) => (cur ? { items: cur.items.filter((r) => r.review_id !== id), total: cur.total === null ? null : Math.max(0, cur.total - 1) } : cur));
  /** UI2-05: Undo from the merge toast: unmerge the subject, then reload the queue (the review may reopen). */
  const undoMerge = (entityId: string, label: string, who: string) => {
    unmergeItem(entityId, { operator: who })
      .then(() => {
        toasts.success(`Unmerged: ${label} is a separate item again.`);
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
      {bulkTab && reviews && reviews.length > 0 && (
        <BulkNotInventory reviews={reviews} selected={selected} setSelected={setSelected} operator={op}
          onDone={(ids) => {
            ids.forEach(drop);
            setSelected((cur) => new Set([...cur].filter((x) => !ids.includes(x))));
            refreshCount();
          }} />
      )}
      <div className="review-list">
        {reviews?.map((r) => (
          <ReviewCard key={r.review_id} review={r} catalog={catalog} nameOf={nameOf} canAct={!!op} resolve={makeResolve(r)}
            sameFrame={sameFrame.get(r.review_id)}
            selection={bulkTab && r.kind === 'unknown_object'
              ? { checked: selected.has(r.review_id), onChange: (on) => setOne(r.review_id, on) }
              : undefined} />
        ))}
      </div>
    </div>
  );
}
