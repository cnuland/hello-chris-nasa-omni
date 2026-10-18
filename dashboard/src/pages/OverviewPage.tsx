// AC-UI-9: overview tiles (items, unknowns, possible duplicates, people seen in the last hour) and a recent events
// feed, refreshed every 10 s while the page is mounted.
import { useCallback, useEffect, useState } from 'react';
import { Link } from 'react-router-dom';
import { getStats, listEvents, listItems } from '../api/client';
import type { Event, Stats } from '../api/types';
import { Timestamp } from '../components/Timestamp';
import { eventActor, eventPlace, humanize, isPersonEvent, personEventSentence, shortId } from '../lib/format';
import { personLabel } from '../lib/person';
import { useToasts } from '../state/toasts';

export const REFRESH_MS = 10_000;
const EVENTS_LIMIT = 25;

function Tile({ label, value, hint }: { label: string; value: number | null; hint: string }) {
  return (
    <section className="tile" aria-label={label}>
      <div className="tile-label" aria-hidden="true">{label}</div>{' '}
      <div className="tile-value">{value === null ? '—' : value}</div>{' '}
      <div className="tile-hint muted">{hint}</div>
    </section>
  );
}

/**
 * Where the event happened, in the shared wording (UI2-09 / UI-13): "at X" for uses and additions, "from A to B" for
 * moves (names when known); '' when neither end is known.
 */
export function eventRoute(e: Event): string {
  return eventPlace(e);
}

/**
 * UI3-04: a person's own track event: "Person: grey t-shirt seen in Cupola (CUP1)", with no "by <actor>" (the actor
 * description of a person's own event is that person's appearance, used for the label when nothing better is sent).
 */
function PersonEventLine({ e }: { e: Event }) {
  const label = personLabel({
    entity_id: e.entity_id ?? '', label: e.entity_label, description: e.entity_description || e.actor_desc,
  });
  return (
    <li className={`event event-person event-${e.event_type ?? 'other'}`}>
      <span className="event-type">person</span>{' '}
      {e.entity_id ? (
        <Link to={`/items/${e.entity_id}`} className="event-entity">
          {label}
          {!label.endsWith(shortId(e.entity_id)) && <span className="muted">{` ${shortId(e.entity_id)}`}</span>}
        </Link>
      ) : label}{' '}
      <span className="event-route">{personEventSentence(e)}</span>{' '}
      <Timestamp iso={e.occurred_at} empty="unknown time" />
    </li>
  );
}

function EventLine({ e }: { e: Event }) {
  if (isPersonEvent(e)) return <PersonEventLine e={e} />;
  const route = eventRoute(e);
  const label = e.entity_label?.trim() || (e.entity_id ? `item ${shortId(e.entity_id)}` : '');
  const who = eventActor(e);
  // A person with no description adds nothing ("by an unknown person"); an operator is always named (UI2-10).
  const actor = e.actor_desc?.trim() || who === 'an operator' ? `by ${who}` : '';
  return (
    <li className={`event event-${e.event_type ?? 'other'}`}>
      <span className="event-type">{humanize(e.event_type) || 'event'}</span>{' '}
      {e.entity_id && (
        <Link to={`/items/${e.entity_id}`} className="event-entity">
          {label}
          {e.entity_label?.trim() && <span className="muted">{` ${shortId(e.entity_id)}`}</span>}
        </Link>
      )}{' '}
      {actor && <span className="event-actor">{actor}</span>}{' '}
      {route && <span className="event-route">{route}</span>}{' '}
      <Timestamp iso={e.occurred_at} empty="unknown time" />
    </li>
  );
}

export default function OverviewPage() {
  const toasts = useToasts();
  const [stats, setStats] = useState<Stats | null>(null);
  const [people, setPeople] = useState<number | null>(null);
  const [events, setEvents] = useState<Event[] | null>(null);

  // Each tick aborts the previous tick's requests, so a slow older response can never overwrite newer data and
  // requests do not pile up when the API is slower than the refresh interval.
  const refresh = useCallback(
    (signal: AbortSignal) => {
      const ok = () => !signal.aborted;
      getStats(signal)
        .then((s) => ok() && setStats(s))
        .catch((e) => ok() && toasts.error('Could not load stats', e));
      const hourAgo = new Date(Date.now() - 3600_000).toISOString();
      listItems({ kind: 'person', seen_after: hourAgo, limit: 1 }, signal)
        .then((p) => ok() && setPeople(p.total))
        .catch((e) => ok() && toasts.error('Could not count people', e));
      listEvents({ limit: String(EVENTS_LIMIT) }, signal)
        .then((ev) => ok() && setEvents(ev))
        .catch((e) => {
          if (!ok()) return;
          setEvents((cur) => cur ?? []);
          toasts.error('Could not load events', e);
        });
    },
    [toasts],
  );

  useEffect(() => {
    let ctrl = new AbortController();
    refresh(ctrl.signal);
    const timer = setInterval(() => {
      ctrl.abort();
      ctrl = new AbortController();
      refresh(ctrl.signal);
    }, REFRESH_MS);
    return () => {
      ctrl.abort();
      clearInterval(timer);
    };
  }, [refresh]);

  // Items = live inventory objects. by_kind already excludes merged/dismissed; by_status covers every entity
  // (including person tracks), so it must not be used here.
  const items = stats ? Number(stats.by_kind?.object ?? 0) || 0 : null;
  const open = stats?.open_reviews_by_kind ?? {};

  return (
    <div className="page overview">
      <h1>Overview</h1>
      <div className="tiles">
        <Tile label="Items" value={items} hint="live objects (excl. people, merged, dismissed)" />
        <Tile label="Unknowns" value={stats ? open.unknown_object ?? 0 : null} hint="open unknown objects" />
        <Tile label="Possible duplicates" value={stats ? open.possible_duplicate ?? 0 : null} hint="open duplicate reviews" />
        <Tile label="Person tracks seen (last hour)" value={people} hint="tracks, not people: one person can have several" />
      </div>
      <section className="card">
        <h2 id="events-h">Recent events</h2>
        {events === null ? (
          <p className="muted">Loading events…</p>
        ) : events.length === 0 ? (
          <p className="empty">No recent events.</p>
        ) : null}
        <ul className="events" aria-label="Recent events">
          {(events ?? []).map((e, i) => <EventLine key={e.event_id ?? i} e={e} />)}
        </ul>
      </section>
    </div>
  );
}
