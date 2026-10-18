// AC-UI-4/5: item detail page (large snapshot, attributes, history timeline, "moved by" lines) + edit form.
import { useEffect, useMemo, useState } from 'react';
import { Link, useLocation, useParams } from 'react-router-dom';
import { ApiError, getHistory, getItem, unmergeItem } from '../api/client';
import type { Event, Item, ItemDetail, ItemPatch, Sighting, TimelineEntry } from '../api/types';
import { ConfidenceBar } from '../components/ConfidenceBar';
import { EditItemForm } from '../components/EditItemForm';
import { FrameImage, isCropUrl, validBBox } from '../components/FrameImage';
import { Timestamp } from '../components/Timestamp';
import { UUID_RE } from '../lib/agent';
import { eventSentence, formatUtc, formatUtcMinute, legacyEventSummary, sentenceCase, shortId } from '../lib/format';
import { LastHandledCell } from '../components/LastHandled';
import { PERSON_FRAME_CAPTION, displayLabel, displayLabelWithId, isPerson, safePersonImage } from '../lib/person';
import { useCatalog, useLocations } from '../state/reference';
import { useOperator } from '../state/operator';
import { errorMessage, useToasts } from '../state/toasts';

type LoadState = { kind: 'loading' } | { kind: 'ok'; item: ItemDetail } | { kind: 'notfound' } | { kind: 'error' };

const DERIVED: Record<string, (keyof Item)[]> = { catalog_id: ['catalog_name'], last_location: ['last_location_name'] };

/** Fresh GET wins, except for the fields just PATCHed (and their display names), taken from the PATCH response. */
function mergeSaved(fresh: ItemDetail, updated: Item, patch: ItemPatch): ItemDetail {
  const out: Record<string, unknown> = { ...fresh };
  for (const k of Object.keys(patch)) {
    if (k === 'operator' || k === 'note') continue;
    for (const key of [k, ...(DERIVED[k] ?? [])]) {
      if (key in updated) out[key] = (updated as unknown as Record<string, unknown>)[key];
    }
  }
  return out as unknown as ItemDetail;
}

function attrValue(v: unknown): string {
  if (v === null || v === undefined) return '';
  return typeof v === 'object' ? JSON.stringify(v) : String(v);
}

type TimelineKind = TimelineEntry['kind'];
const KIND_LABELS: Record<TimelineKind, string> = { sighting: 'Sightings', event: 'Events', audit: 'Audit' };

/** A rendered history row: one entry, or a run of repeated audit rows folded together (AC-UI-17). */
export interface TimelineGroup {
  entry: TimelineEntry;
  count: number;
  firstAt: string;
  members: TimelineEntry[];
}

/**
 * Folds consecutive audit entries with the same summary (e.g. "update by pipeline") into one row. Entries the API
 * already collapsed (count > 1) keep their count; a run of API-collapsed and raw rows adds up.
 */
export function groupTimeline(entries: TimelineEntry[]): TimelineGroup[] {
  const out: TimelineGroup[] = [];
  for (const e of entries) {
    const n = typeof e.count === 'number' && e.count > 1 ? Math.floor(e.count) : 1;
    const prev = out[out.length - 1];
    if (prev && e.kind === 'audit' && prev.entry.kind === 'audit' && (prev.entry.summary ?? '') === (e.summary ?? '') && !e.image_url) {
      prev.count += n;
      prev.firstAt = e.first_at ?? e.at;
      prev.members.push(e);
      continue;
    }
    out.push({ entry: e, count: n, firstAt: e.first_at ?? e.at, members: [e] });
  }
  return out;
}

/** Audit ops (inventory-api audit_log.op) as sentence-case past-tense labels (UI3-11). */
const AUDIT_OPS: Record<string, string> = {
  merge: 'Merged', split: 'Unmerged', unmerge: 'Unmerged', create: 'Created', update: 'Updated', dismiss: 'Dismissed',
  classify: 'Classified', delete: 'Deleted', purge: 'Purged',
};

/**
 * UI3-11: history wording in sentence case: "merge by X" reads "Merged by X", "split by X" reads "Unmerged by X",
 * "create by pipeline" reads "Created by pipeline", and "update by pipeline" reads "Pipeline update". Any detail
 * after the actor (changes, note) is kept. UI5-10: "split" always reads "Unmerged", with or without an actor.
 */
export function auditText(summary: string | null | undefined): string {
  const s = (summary ?? '').trim();
  if (/^update by pipeline$/i.test(s)) return 'Pipeline update';
  // UI5-10: the op may stand alone ("split") or be followed by " by X", ": note" or " (changes)".
  const m = /^([a-z_]+)(?=$| by\b|:| \()/i.exec(s);
  const label = m ? AUDIT_OPS[m[1].toLowerCase()] : undefined;
  if (m && label) return `${label}${s.slice(m[1].length)}`;
  return s ? s[0].toUpperCase() + s.slice(1) : s;
}

/** Seconds within which a pipeline update counts as the mirror of a sighting (the sighting refresh it records). */
export const MIRROR_WINDOW_MS = 2000;

/**
 * UI3-11: drops single "Pipeline update" audit rows that only mirror a sighting at the same time (every sighting
 * refreshes last_seen_at/confidence and writes one). Server-collapsed rows (count > 1) and other audits stay.
 */
export function hideMirroredAudits(entries: TimelineEntry[]): TimelineEntry[] {
  const seen = entries.filter((e) => e.kind === 'sighting').map((e) => Date.parse(e.at)).filter((t) => !Number.isNaN(t));
  if (!seen.length) return entries;
  return entries.filter((e) => {
    if (e.kind !== 'audit' || (typeof e.count === 'number' && e.count > 1)) return true;
    if (auditText(e.summary) !== 'Pipeline update') return true;
    const t = Date.parse(e.at);
    return Number.isNaN(t) || !seen.some((x) => Math.abs(x - t) <= MIRROR_WINDOW_MS);
  });
}

/**
 * Row text (UI2-11, UI3-11): "14 pipeline updates" / "14 × <summary>" for folded runs. A server-collapsed summary already starts with its
 * count ("9 pipeline updates (…)"), so that number is replaced by the group total instead of prefixing "9 × ".
 */
export function timelineLabel(g: TimelineGroup): string {
  const s = g.entry.kind === 'audit' ? auditText(g.entry.summary) : g.entry.summary || '';
  if (g.count <= 1) return s;
  if (s === 'Pipeline update') return `${g.count} pipeline updates`;
  const m = /^(\d+)\s+(.+)$/.exec(s);
  return m ? `${g.count} ${m[2]}` : `${g.count} × ${s || 'audit entry'}`;
}

/** Context from the item detail used to render history rows in the shared wording (UI-04) and with boxes (UI2-07). */
interface RowContext {
  label: string;
  /** UI3-04: the item is a person: its own events read "left X" / "seen in X", with no actor. */
  person: boolean;
  /** UI4-06: the item's current location ("left X" reads "seen in X" while the person is still in X). */
  lastLocation?: string | null;
  lastLocationName?: string | null;
  events: Map<string, Event>;
  sightings: Map<string, Sighting>;
}

/** History row text: event rows use the Activity sentence (matched by event id, else rewritten from the summary). */
function rowText(g: TimelineGroup, ctx: RowContext): string {
  const e = g.entry;
  if (e.kind !== 'event' || g.count > 1) return timelineLabel(g);
  const ev = e.id ? ctx.events.get(e.id) : undefined;
  if (ev) return eventSentence(ev, { person: ctx.person, lastLocation: ctx.lastLocation, lastLocationName: ctx.lastLocationName });
  return legacyEventSummary(e.summary, { source: e.source, actor_kind: e.actor_kind, location_role: e.location_role }, { person: ctx.person })
    ?? e.summary ?? '';
}

function TimelineRow({ g, ctx }: { g: TimelineGroup; ctx: RowContext }) {
  const [open, setOpen] = useState(false);
  const e = g.entry;
  const label = ctx.label;
  const folded = g.count > 1;
  // UI6-01: the API returns integer sighting ids; key and look up as strings on both sides
  const sighting = e.kind === 'sighting' && e.id != null ? ctx.sightings.get(String(e.id)) : undefined;
  const text = rowText(g, ctx);
  return (
    <li className={`tl tl-${e.kind}`}>
      <div className="tl-head">
        <span className={`tl-kind tl-kind-${e.kind}`}>{e.kind}</span>
        <time dateTime={e.at}>{formatUtc(e.at)}</time>
        {folded && g.firstAt !== e.at && <span className="muted">{`back to ${formatUtc(g.firstAt)}`}</span>}
        {e.location_id && <code className="loc-code">{e.location_id}</code>}
      </div>
      <p className="tl-summary">{text}</p>
      {folded && g.members.length > 1 && (
        <>
          <button type="button" className="link-button" aria-expanded={open} onClick={() => setOpen((v) => !v)}>
            {open ? 'Hide individual entries' : `Show ${g.members.length} entries`}
          </button>
          {open && (
            <ul className="tl-members" aria-label="Folded entries">
              {g.members.map((m, i) => (
                <li key={`${m.at}-${i}`}><time dateTime={m.at}>{formatUtc(m.at)}</time>{` ${m.kind === 'audit' ? auditText(m.summary) : m.summary ?? ''}`}</li>
              ))}
            </ul>
          )}
        </>
      )}
      {/* UI5-06: every frame opens the lightbox; with no box it says "whole frame" (persons: the privacy caption). */}
      {e.image_url && (
        <FrameImage src={e.image_url} alt={`Evidence for ${label}: ${text || e.kind}`} bbox={sighting?.bbox}
          bboxSource={sighting?.bbox_source} size="md" person={ctx.person}
          noBoxCaption={ctx.person ? PERSON_FRAME_CAPTION : undefined} />
      )}
    </li>
  );
}

function Timeline({ entries, ctx }: { entries: TimelineEntry[] | null; ctx: RowContext }) {
  const [hidden, setHidden] = useState<Set<TimelineKind>>(() => new Set());
  const shown = useMemo(() => (entries ? hideMirroredAudits(entries) : null), [entries]);
  const groups = useMemo(
    () => groupTimeline((shown ?? []).filter((e) => !hidden.has(e.kind))),
    [shown, hidden],
  );
  if (shown === null) return <p className="muted">Loading history…</p>;
  if (shown.length === 0) return <p className="empty">No history recorded yet.</p>;
  const counts = shown.reduce<Record<string, number>>((acc, e) => {
    acc[e.kind] = (acc[e.kind] ?? 0) + (typeof e.count === 'number' && e.count > 1 ? e.count : 1);
    return acc;
  }, {});
  const toggle = (k: TimelineKind) =>
    setHidden((cur) => {
      const next = new Set(cur);
      if (next.has(k)) next.delete(k);
      else next.add(k);
      return next;
    });
  return (
    <>
      <div className="chips" role="group" aria-label="Show history entries">
        {(Object.keys(KIND_LABELS) as TimelineKind[]).map((k) => (
          <button key={k} type="button" className="chip" aria-pressed={!hidden.has(k)} onClick={() => toggle(k)}>
            {`${KIND_LABELS[k]} (${counts[k] ?? 0})`}
          </button>
        ))}
      </div>
      {groups.length === 0 ? (
        <p className="muted">All entry kinds are hidden.</p>
      ) : (
        <ol className="timeline" aria-label="History">
          {groups.map((g, i) => <TimelineRow key={`${g.entry.at}-${i}`} g={g} ctx={ctx} />)}
        </ol>
      )}
    </>
  );
}

/**
 * UI2-05: banner on a merged item: where it went and when, plus Unmerge for operators. A 409 (the merge can no
 * longer be undone safely) is explained inline and the banner stays.
 */
function MergedBanner({ item, onUnmerged }: { item: ItemDetail; onUnmerged: () => void }) {
  const toasts = useToasts();
  const [operator, setOperator] = useOperator();
  const [busy, setBusy] = useState(false);
  const [conflict, setConflict] = useState<string | null>(null);
  const op = operator.trim();
  const target = item.merged_into;
  // UI3-11: items are named with their short id in banners and toasts (two items often share a label).
  const targetLabel = target ? displayLabelWithId({ entity_id: target.entity_id, label: target.label }) : '';
  const unmerge = async () => {
    if (!op || busy) return;
    setBusy(true);
    setConflict(null);
    try {
      await unmergeItem(item.entity_id, { operator: op });
      toasts.success(`Unmerged: ${displayLabelWithId(item)} is a separate item again.`);
      onUnmerged();
    } catch (e) {
      if (e instanceof ApiError && e.status === 409) {
        setConflict(`This merge can't be undone safely: ${errorMessage(e)}. Nothing was changed.`);
      } else {
        toasts.error('Could not unmerge', e);
      }
    } finally {
      setBusy(false);
    }
  };
  return (
    <section className="card merged-banner" aria-label="Merged item">
      <p className="merged-text">
        {target ? (
          <>
            {'Merged into '}
            <Link to={`/items/${target.entity_id}`}>{targetLabel}</Link>
            {item.merged_at && (
              <>
                {' at '}
                <time dateTime={item.merged_at} title={formatUtc(item.merged_at)}>{formatUtcMinute(item.merged_at)}</time>
              </>
            )}
            {'. Its sightings and history now belong to that item.'}
          </>
        ) : (
          'This item was merged into another item. Its sightings and history moved there.'
        )}
      </p>
      <div className="merged-actions">
        <div className="field">
          <label htmlFor="unmerge-op">Operator</label>
          <input id="unmerge-op" value={operator} autoComplete="name" onChange={(e) => setOperator(e.target.value)} />
        </div>
        <button type="button" disabled={!op || busy} aria-describedby="unmerge-hint" onClick={unmerge}>
          {busy ? 'Unmerging…' : 'Unmerge'}
        </button>
        <span id="unmerge-hint" className="muted small">
          {op ? 'Unmerge restores this item and moves its sightings back.' : 'Enter an operator name to unmerge.'}
        </span>
      </div>
      {conflict && <p role="alert" className="field-error">{conflict}</p>}
    </section>
  );
}

function MergedFromList({ item }: { item: ItemDetail }) {
  const from = (item.merged_from ?? []).filter((m) => m && typeof m.entity_id === 'string');
  if (!from.length) return null;
  return (
    <>
      <h2 id="merged-from-h">Merged from</h2>
      <ul className="merged-from" aria-labelledby="merged-from-h">
        {from.map((m) => (
          <li key={m.entity_id}>
            <Link to={`/items/${m.entity_id}`} title={m.label?.trim() || undefined}>
              {sentenceCase(m.label?.trim()) || `item ${shortId(m.entity_id)}`}
            </Link>{' '}
            <code className="loc-code">{shortId(m.entity_id)}</code>
            {m.merged_at && (
              <>
                {' · merged '}
                <time dateTime={m.merged_at}>{formatUtcMinute(m.merged_at)}</time>
              </>
            )}
          </li>
        ))}
      </ul>
    </>
  );
}

/** UI4-11: a crop whose long side is under this many pixels is too small to be the hero image. */
export const SMALL_CROP_PX = 200;

/** UI4-10: text shown instead of "no image" when the API says why the snapshot is withheld. */
export const WITHHELD_TEXT: Record<string, string> = {
  private_window: 'No image: first seen in a private area (crew privacy)',
};

/** The best whole frame of the item: a sighting frame with a box first, else the most recent frame (never a crop). */
export function bestFrame(sightings: Sighting[] | null | undefined, person: boolean): Sighting | null {
  const frames = (sightings ?? []).filter((x) => x && typeof x.frame_url === 'string' && x.frame_url && !isCropUrl(x.frame_url)
    && (!person || safePersonImage(x.frame_url)));
  const newest = (a: Sighting, b: Sighting) => (Date.parse(b.seen_at ?? '') || 0) - (Date.parse(a.seen_at ?? '') || 0);
  const boxed = frames.filter((x) => validBBox(x.bbox)).sort(newest);
  return boxed[0] ?? [...frames].sort(newest)[0] ?? null;
}

/**
 * The detail hero (UI3-07, UI4-10, UI4-11). A crop under SMALL_CROP_PX on its long side is shown as an inset
 * "close-up" under the item's best whole frame; with no frame it is shown at up to 2× with a "small close-up" caption.
 */
function DetailHero({ item, title, person }: { item: ItemDetail; title: string; person: boolean }) {
  const [natural, setNatural] = useState<[number, number] | null>(null);
  const crop = isCropUrl(item.snapshot_url);
  const small = crop && !!natural && Math.max(natural[0], natural[1]) > 0 && Math.max(natural[0], natural[1]) < SMALL_CROP_PX;
  const frame = small ? bestFrame(item.sightings, person) : null;
  const withheld = item.snapshot_withheld_reason ? WITHHELD_TEXT[item.snapshot_withheld_reason] : undefined;
  // UI5-10: a person's frame is never boxed: say why instead of "there is no box for this object".
  const noBox = person ? PERSON_FRAME_CAPTION : undefined;
  if (small && frame) {
    return (
      <>
        <div className="hero-frame">
          <FrameImage src={frame.frame_url as string} alt={`${title}: whole frame`} size="lg" bbox={frame.bbox}
            bboxSource={frame.bbox_source} caption person={person} noBoxCaption={noBox} />
        </div>
        <div className="hero-inset">
          <FrameImage src={item.snapshot_url} alt={`${title}: close-up`} size="md" crop person={person} />
          <p className="hero-inset-text muted small">
            {`Close-up of the object (${natural![0]}×${natural![1]} px, too small to show on its own). Click to enlarge.`}
          </p>
        </div>
      </>
    );
  }
  return (
    <FrameImage src={item.snapshot_url} alt={title} size="lg" bbox={item.snapshot_bbox} bboxSource={item.snapshot_bbox_source}
      caption person={person} emptyText={withheld} noBoxCaption={noBox}
      upscale={crop ? (w, h) => Math.max(w, h) < SMALL_CROP_PX && !bestFrame(item.sightings, person) : false}
      captionText={small ? 'Small close-up (shown at up to 2×; no whole frame is available). Click to enlarge.' : undefined}
      onNaturalSize={crop ? (w, h) => setNatural((cur) => (cur && cur[0] === w && cur[1] === h ? cur : [w, h])) : undefined} />
  );
}

export default function ItemDetailPage() {
  const { id = '' } = useParams();
  const toasts = useToasts();
  const { options: locationOptions } = useLocations();
  const catalog = useCatalog();
  const [state, setState] = useState<LoadState>({ kind: 'loading' });
  const [history, setHistory] = useState<TimelineEntry[] | null>(null);
  const [editing, setEditing] = useState(false);

  useEffect(() => {
    let live = true;
    setState({ kind: 'loading' });
    setHistory(null);
    setEditing(false);
    if (!UUID_RE.test(id)) {
      // A malformed id can never exist (the API answers 422): show "not found" without a request (AC-UI-23).
      setState({ kind: 'notfound' });
      setHistory([]);
      return () => {
        live = false;
      };
    }
    getItem(id)
      .then((item) => live && setState({ kind: 'ok', item }))
      .catch((e) => {
        if (!live) return;
        if (e instanceof ApiError && (e.status === 404 || e.status === 422)) setState({ kind: 'notfound' });
        else {
          setState({ kind: 'error' });
          toasts.error('Could not load item', e);
        }
      });
    getHistory(id)
      .then((h) => live && setHistory(h))
      .catch((e) => {
        if (!live) return;
        setHistory([]);
        if (!(e instanceof ApiError && (e.status === 404 || e.status === 422))) toasts.error('Could not load history', e);
      });
    return () => {
      live = false;
    };
  }, [id, toasts]);

  // Deep link from the review queue: /items/:id#history scrolls to the history once it has loaded.
  const { hash } = useLocation();
  useEffect(() => {
    if (hash === '#history' && history !== null && state.kind === 'ok') {
      document.getElementById('history')?.scrollIntoView({ block: 'start' });
    }
  }, [hash, history, state.kind]);

  const reloadHistory = () => {
    getHistory(id).then(setHistory).catch(() => undefined);
  };
  const reloadItem = () => {
    getItem(id)
      .then((fresh) => setState({ kind: 'ok', item: fresh }))
      .catch(() => undefined);
    reloadHistory();
  };

  if (state.kind === 'loading') return <p className="muted">Loading item…</p>;
  if (state.kind === 'notfound') {
    return (
      <div className="page">
        <h1>Item not found</h1>
        <p className="empty">{`No item has the id ${id.slice(0, 80)}.`}</p>
        <Link to="/">Back to inventory</Link>
      </div>
    );
  }
  if (state.kind === 'error') {
    return (
      <div className="page">
        <h1>Item unavailable</h1>
        <p className="empty">This item could not be loaded.</p>
        <Link to="/">Back to inventory</Link>
      </div>
    );
  }

  const item = state.item;
  const activity = [...(item.events ?? [])].sort(
    (a, b) => (Date.parse(b.occurred_at ?? '') || 0) - (Date.parse(a.occurred_at ?? '') || 0),
  );
  const attributes = Object.entries(item.attributes ?? {});
  const person = isPerson(item);
  const title = displayLabel(item);
  const rowCtx: RowContext = {
    label: title,
    person,
    lastLocation: item.last_location,
    lastLocationName: item.last_location_name,
    events: new Map((item.events ?? []).filter((e) => e.event_id).map((e) => [e.event_id as string, e])),
    sightings: new Map(
      (item.sightings ?? [])
        .filter((x) => x && (typeof x.sighting_id === 'string' || typeof x.sighting_id === 'number'))
        .map((x) => [String(x.sighting_id), x]),
    ),
  };
  const isMerged = item.status === 'merged' || !!item.merged_into;
  const onSaved = (updated: Item, patch: ItemPatch) => {
    setState({ kind: 'ok', item: { ...item, ...updated } });
    setEditing(false);
    reloadHistory();
    // Reload the detail so derived data (Movements from events, last moved by) reflects the edit. The fields this
    // save changed keep the PATCH response values; everything else comes from the fresh GET.
    getItem(id)
      .then((fresh) =>
        setState((cur) => (cur.kind === 'ok' ? { kind: 'ok', item: mergeSaved(fresh, updated, patch) } : cur)),
      )
      .catch(() => undefined);
  };

  return (
    <div className="page detail">
      <div className="page-head">
        <div>
          <Link to="/" className="muted">← Inventory</Link>
          <h1 title={!person && item.label && item.label !== title ? item.label : undefined}>{title}</h1>
        </div>
        {!editing && (
          <button type="button" className="primary" onClick={() => setEditing(true)}>Edit item</button>
        )}
      </div>

      {isMerged && <MergedBanner item={item} onUnmerged={reloadItem} />}

      <div className="detail-grid">
        <div className="detail-media" data-testid="detail-media">
          <DetailHero key={item.snapshot_url ?? ''} item={item} title={title} person={person} />
        </div>
        <div className="card">
          <dl className="facts">
            <dt>Catalog</dt>
            <dd>{item.catalog_name || item.catalog_id || 'Uncatalogued'}</dd>
            <dt>Kind</dt>
            <dd>{item.kind}</dd>
            <dt>Status</dt>
            <dd><span className={`status status-${item.status}`}>{item.status}</span>
              {item.needs_review && <span className="flag">Needs review</span>}</dd>
            <dt>Confidence</dt>
            <dd><ConfidenceBar value={item.confidence} /></dd>
            <dt>Description</dt>
            <dd>{item.description || '—'}</dd>
            <dt>Last location</dt>
            <dd>
              {item.last_location ? `${item.last_location_name || item.last_location} (${item.last_location})` : '—'}
            </dd>
            <dt>Last sighting</dt>
            <dd><Timestamp iso={item.last_seen_at} /></dd>
            {/* UI5-09: a person track is never "handled": it is last seen somewhere. */}
            <dt>{person ? 'Last seen' : 'Last handled'}</dt>
            <dd><LastHandledCell item={item} /></dd>
            {item.location_set_at && (
              <>
                <dt>Location set</dt>
                <dd>
                  {`by ${item.location_set_by || 'unknown'} at `}
                  <time dateTime={item.location_set_at}>{formatUtcMinute(item.location_set_at)}</time>
                </dd>
              </>
            )}
            {item.exemplar_count !== undefined && (
              <>
                <dt>Exemplars</dt>
                <dd>{item.exemplar_count}</dd>
              </>
            )}
          </dl>
          <MergedFromList item={item} />
          <h2>Attributes</h2>
          {attributes.length ? (
            <dl className="attrs">
              {attributes.map(([k, v]) => (
                <div key={k} className="attr">
                  <dt>{k}</dt>
                  <dd>{attrValue(v)}</dd>
                </div>
              ))}
            </dl>
          ) : (
            <p className="muted">No attributes.</p>
          )}
        </div>
      </div>

      {editing && (
        <EditItemForm item={item} catalog={catalog} locations={locationOptions} onSaved={onSaved} onCancel={() => setEditing(false)} />
      )}

      <section className="card" aria-labelledby="moves-h">
        <h2 id="moves-h">Activity</h2>
        {activity.length ? (
          <ul className="moves" aria-label="Activity">
            {activity.map((m, i) => {
              const sentence = eventSentence(m, { person, lastLocation: item.last_location, lastLocationName: item.last_location_name });
              return (
                <li key={m.event_id ?? i} className="activity">
                  <span>{`${sentence} · ${formatUtc(m.occurred_at) || 'unknown time'}`}</span>
                  {m.evidence_url && <FrameImage src={m.evidence_url} alt={`Evidence: ${sentence}`} size="md" person={person}
                    noBoxCaption={person ? PERSON_FRAME_CAPTION : undefined} />}
                </li>
              );
            })}
          </ul>
        ) : (
          <p className="muted">No recorded activity (moves, uses or hand-overs).</p>
        )}
      </section>

      <section className="card" id="history" aria-labelledby="hist-h">
        <h2 id="hist-h">History</h2>
        <Timeline entries={history} ctx={rowCtx} />
      </section>
    </div>
  );
}
