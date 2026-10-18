// AC-UI-4/5: item detail page (large snapshot, attributes, history timeline, "moved by" lines) + edit form.
import { useEffect, useMemo, useState } from 'react';
import { Link, useLocation, useParams } from 'react-router-dom';
import { ApiError, getHistory, getItem } from '../api/client';
import type { Event, Item, ItemDetail, ItemPatch, TimelineEntry } from '../api/types';
import { ConfidenceBar } from '../components/ConfidenceBar';
import { EditItemForm } from '../components/EditItemForm';
import { FrameImage } from '../components/FrameImage';
import { Thumb } from '../components/Thumb';
import { Timestamp } from '../components/Timestamp';
import { UUID_RE } from '../lib/agent';
import { formatUtc, formatUtcMinute, handledPhrase, locationLabel } from '../lib/format';
import { LastHandledCell } from '../components/LastHandled';
import { useCatalog, useLocations } from '../state/reference';
import { useToasts } from '../state/toasts';

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

function TimelineRow({ g, label }: { g: TimelineGroup; label: string }) {
  const [open, setOpen] = useState(false);
  const e = g.entry;
  const folded = g.count > 1;
  return (
    <li className={`tl tl-${e.kind}`}>
      <div className="tl-head">
        <span className={`tl-kind tl-kind-${e.kind}`}>{e.kind}</span>
        <time dateTime={e.at}>{formatUtc(e.at)}</time>
        {folded && g.firstAt !== e.at && <span className="muted">{`back to ${formatUtc(g.firstAt)}`}</span>}
        {e.location_id && <code className="loc-code">{e.location_id}</code>}
      </div>
      <p className="tl-summary">{folded ? `${g.count} × ${e.summary || 'audit entry'}` : e.summary || ''}</p>
      {folded && g.members.length > 1 && (
        <>
          <button type="button" className="link-button" aria-expanded={open} onClick={() => setOpen((v) => !v)}>
            {open ? 'Hide individual entries' : `Show ${g.members.length} entries`}
          </button>
          {open && (
            <ul className="tl-members" aria-label="Folded entries">
              {g.members.map((m, i) => (
                <li key={`${m.at}-${i}`}><time dateTime={m.at}>{formatUtc(m.at)}</time>{` ${m.summary ?? ''}`}</li>
              ))}
            </ul>
          )}
        </>
      )}
      {e.image_url && <Thumb src={e.image_url} alt={`Evidence for ${label}: ${e.summary ?? e.kind}`} size="md" />}
    </li>
  );
}

function Timeline({ entries, label }: { entries: TimelineEntry[] | null; label: string }) {
  const [hidden, setHidden] = useState<Set<TimelineKind>>(() => new Set());
  const groups = useMemo(
    () => groupTimeline((entries ?? []).filter((e) => !hidden.has(e.kind))),
    [entries, hidden],
  );
  if (entries === null) return <p className="muted">Loading history…</p>;
  if (entries.length === 0) return <p className="empty">No history recorded yet.</p>;
  const counts = entries.reduce<Record<string, number>>((acc, e) => {
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
          {groups.map((g, i) => <TimelineRow key={`${g.entry.at}-${i}`} g={g} label={label} />)}
        </ol>
      )}
    </>
  );
}

/** Activity line (AC-UI-12): "Moved by person (blue shirt) at 2026-10-17 11:00:00 UTC, from A to B". */
function activityText(e: Event): string {
  const head = `${handledPhrase(e.event_type, e.actor_desc)} at ${formatUtc(e.occurred_at) || 'an unknown time'}`;
  const from = e.from_location ? locationLabel(e.from_location, e.from_location_name) : '';
  const to = e.to_location ? locationLabel(e.to_location, e.to_location_name) : '';
  const type = (e.event_type ?? '').toLowerCase();
  if (type === 'moved' || from || to) {
    if (from && to) return `${head}, from ${from} to ${to}`;
    if (from) return `${head}, from ${from} to an unknown location`;
    if (to) return type === 'added' ? `${head}, at ${to}` : `${head}, to ${to}`;
  }
  return head;
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

  if (state.kind === 'loading') return <p className="muted">Loading item…</p>;
  if (state.kind === 'notfound') {
    return (
      <div className="page">
        <p className="empty">Item not found.</p>
        <Link to="/">Back to inventory</Link>
      </div>
    );
  }
  if (state.kind === 'error') {
    return (
      <div className="page">
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
          <h1>{item.label}</h1>
        </div>
        {!editing && (
          <button type="button" className="primary" onClick={() => setEditing(true)}>Edit item</button>
        )}
      </div>

      <div className="detail-grid">
        <div className="detail-media">
          <FrameImage src={item.snapshot_url} alt={item.label} size="lg" />
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
            <dt>Last handled</dt>
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
            {activity.map((m, i) => (
              <li key={m.event_id ?? i} className="activity">
                <span>{activityText(m)}</span>
                {m.evidence_url && (
                  <FrameImage src={m.evidence_url} alt={`Evidence: ${activityText(m)}`} size="md" />
                )}
              </li>
            ))}
          </ul>
        ) : (
          <p className="muted">No recorded activity (moves, uses or hand-overs).</p>
        )}
      </section>

      <section className="card" id="history" aria-labelledby="hist-h">
        <h2 id="hist-h">History</h2>
        <Timeline entries={history} label={item.label} />
      </section>
    </div>
  );
}
