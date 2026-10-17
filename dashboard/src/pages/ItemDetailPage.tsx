// AC-UI-4/5: item detail page (large snapshot, attributes, history timeline, "moved by" lines) + edit form.
import { useEffect, useState } from 'react';
import { Link, useParams } from 'react-router-dom';
import { ApiError, getHistory, getItem } from '../api/client';
import type { Item, ItemDetail, ItemPatch, TimelineEntry } from '../api/types';
import { ConfidenceBar } from '../components/ConfidenceBar';
import { EditItemForm } from '../components/EditItemForm';
import { Thumb } from '../components/Thumb';
import { formatRelative, formatUtc } from '../lib/format';
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

function Timeline({ entries, label }: { entries: TimelineEntry[] | null; label: string }) {
  if (entries === null) return <p className="muted">Loading history…</p>;
  if (entries.length === 0) return <p className="empty">No history recorded yet.</p>;
  return (
    <ol className="timeline" aria-label="History">
      {entries.map((e, i) => (
        <li key={`${e.at}-${i}`} className={`tl tl-${e.kind}`}>
          <div className="tl-head">
            <span className={`tl-kind tl-kind-${e.kind}`}>{e.kind}</span>
            <time dateTime={e.at}>{formatUtc(e.at)}</time>
            {e.location_id && <code className="loc-code">{e.location_id}</code>}
          </div>
          <p className="tl-summary">{e.summary || ''}</p>
          {e.image_url && <Thumb src={e.image_url} alt={`Evidence for ${label}: ${e.summary ?? e.kind}`} size="md" />}
        </li>
      ))}
    </ol>
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
    getItem(id)
      .then((item) => live && setState({ kind: 'ok', item }))
      .catch((e) => {
        if (!live) return;
        if (e instanceof ApiError && e.status === 404) setState({ kind: 'notfound' });
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
        if (!(e instanceof ApiError && e.status === 404)) toasts.error('Could not load history', e);
      });
    return () => {
      live = false;
    };
  }, [id, toasts]);

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
  const moves = (item.events ?? []).filter((e) => e.event_type === 'moved');
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
          <Thumb src={item.snapshot_url} alt={item.label} size="lg" />
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
            <dt>Last seen</dt>
            <dd title={formatUtc(item.last_seen_at) || undefined}>{formatRelative(item.last_seen_at)}</dd>
            <dt>Last moved by</dt>
            <dd>{item.last_moved_by || '—'}</dd>
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
        <h2 id="moves-h">Movements</h2>
        {moves.length ? (
          <ul className="moves" aria-label="Movements">
            {moves.map((m, i) => (
              <li key={m.event_id ?? i}>
                <span>{`Moved by ${m.actor_desc || 'unknown'} at ${formatUtc(m.occurred_at)}`}</span>
                {(m.from_location || m.to_location) && (
                  <span className="muted">{` ${m.from_location || '?'} → ${m.to_location || '?'}`}</span>
                )}
              </li>
            ))}
          </ul>
        ) : (
          <p className="muted">No recorded movements.</p>
        )}
      </section>

      <section className="card" aria-labelledby="hist-h">
        <h2 id="hist-h">History</h2>
        <Timeline entries={history} label={item.label} />
      </section>
    </div>
  );
}
